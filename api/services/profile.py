"""The profile: is this person ready, and are they coming back.

Everything here is derived from rows that already exist — `events` for the streak and for
accuracy, `quiz_sessions` for exam history. Nothing new is stored, which matters because a
"streak" column would need a nightly job to decay it and would be wrong for anyone in a
different timezone than the job.

Readiness read `progress` until 2026-07-31, and `progress` is a per-question RUNNING TOTAL,
not an answer log. The difference is not academic: see `_readiness`.

The number this screen lives or dies on is READINESS, and it is a claim made to someone
about to pay money to sit a real exam. So it is defined narrowly and refuses to answer
when it does not know:

  · It is accuracy over recent answers, not over all time. Someone who was at 40% a month
    ago and is at 85% now is ready; an all-time average says otherwise and is useless.
  · It is null below MIN_SAMPLE answers. A user who has answered four questions correctly
    is not "100% ready", and printing that would be a lie the product tells at exactly
    the moment it is asking to be trusted.
  · It is not scaled by coverage. Coverage is reported separately (`questions_seen`)
    rather than blended in, because a single number that secretly mixes two things is a
    number nobody can act on.

The real exam tolerates 3 errors in 30, i.e. 90%. That threshold is reported alongside
so the percentage has something to mean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, QuizSession, User
from api.services import streak as streak_service
from shared.constants import (
    EV_ANSWER_GIVEN,
    EXAM_MAX_ERRORS,
    EXAM_QUESTIONS,
    MODE_EXAM,
    SESSION_OPEN,
)

# Below this, readiness is None rather than a flattering guess.
#
# Was 20. Twenty answers is 0.28% of a 7106-question bank — one good evening, or one
# lucky run on a topic someone happens to know. Putting a percentage on a gauge after
# that, about a legally required exam that costs money and a re-sit to fail, is a claim
# the data cannot support. A learner who is told "84% ready" on twenty answers and then
# fails has been actively misled by the thing they were revising with.
#
# 100 is roughly three mock exams' worth. Still a small window — it is deliberately a
# RECENT-form measure, not a coverage measure — but it is enough that one lucky run
# cannot carry it, and the sample size now travels with the number so the app can say
# what it is based on.
MIN_SAMPLE = 100
# How many recent answers the estimate looks at.
RECENT_WINDOW = 100

# Past this, an answer no longer says anything about how ready somebody is TODAY.
#
# Without it, a learner returning after a long gap keeps their old reading until they have
# ground out RECENT_WINDOW fresh answers — the exact case that makes a stale gauge
# dangerous, since the person most likely to check "am I ready" is the person who has just
# come back. When too few recent answers remain the gauge returns None and the app says it
# does not know yet, which is the honest answer and the one the None case exists for.
STALE_AFTER = timedelta(days=90)
# The bar the percentage is measured against: 27 of 30 correct.
PASS_ACCURACY = round((EXAM_QUESTIONS - EXAM_MAX_ERRORS) / EXAM_QUESTIONS, 3)


async def _readiness(
    session: AsyncSession, chat_id: int, now: datetime | None = None
) -> tuple[float | None, int]:
    """Accuracy over this learner's last RECENT_WINDOW answers.

    THE LAST HUNDRED ANSWERS, NOT A HUNDRED LIFETIME TOTALS

    This read `progress`, which keeps a per-question RUNNING TOTAL rather than an answer
    log, and summed `seen` and `wrong` over the 100 most recently touched questions. It was
    documented as an approximation. It is not an approximation of recent accuracy — it is
    a different quantity, and it fails in the direction that matters:

      · It can never really fall. A question answered wrong four times last month and right
        today still contributes 5 seen / 4 wrong forever, so mistakes the learner has since
        FIXED go on dragging the number down, and there is no action that clears them.
      · It can go UP after a wrong answer. Measured: 0.748 to 0.754. Answering reorders the
        window by `last_answer_at`, so a fresh wrong answer can evict a question with a
        worse lifetime ratio and lift the total. A gauge that rises when you get something
        wrong is not merely imprecise, it is unusable.
      · Somebody returning after two months, getting 30 of 30 wrong, still reads 94% —
        because ninety-odd stale lifetime totals outvote today's thirty answers.

    That last one is the whole risk. This number is a claim made to someone deciding
    whether to book a legally required exam that costs money and a re-sit to fail, and the
    module docstring already promises exactly the right thing: "accuracy over recent
    answers, not over all time". The code did the opposite.

    `events` has carried one row per answer since the first commit, with `correct` in the
    payload, indexed by (chat_id, created_at). So the real rolling window was already on
    disk; nothing needed storing.

    STALE ANSWERS EXPIRE RATHER THAN LINGER

    A pure "last 100" would still hand a returning learner a reading built from answers
    given months ago until they had ground out 100 fresh ones. Past STALE_AFTER the answers
    simply stop counting, and if that leaves fewer than MIN_SAMPLE the gauge goes back to
    saying nothing — which is the honest output for "I do not know how you are doing now",
    and is what the None case exists for.

    EXAM ANSWERS COUNT

    Deliberately not filtered on `graded`. An exam is the most representative sample of
    exam performance there is, and excluding it would mean the one activity that most
    resembles the real thing had no effect on the readiness estimate. It stays out of the
    Leitner SCHEDULE (see answers.py) — that is a separate concern about what to teach
    next, not about how ready somebody is.
    """
    now = now or datetime.now(UTC)
    rows = (
        await session.scalars(
            select(Event.payload)
            .where(
                Event.chat_id == chat_id,
                Event.type == EV_ANSWER_GIVEN,
                Event.created_at > now - STALE_AFTER,
            )
            .order_by(Event.created_at.desc())
            .limit(RECENT_WINDOW)
        )
    ).all()

    answers = [bool((payload or {}).get("correct")) for payload in rows]
    sample = len(answers)
    if sample < MIN_SAMPLE:
        return None, sample
    return round(sum(answers) / sample, 3), sample


async def _exams(session: AsyncSession, chat_id: int) -> dict:
    rows = list(
        await session.scalars(
            select(QuizSession)
            .where(
                QuizSession.chat_id == chat_id,
                QuizSession.mode == MODE_EXAM,
                QuizSession.state != SESSION_OPEN,
                # "Taken" means SAT — submitted, or run out of time. Not "started and
                # walked away from", which is what an abandoned row is and which `_grade`
                # leaves ungraded.
                #
                # IN SQL, not in Python afterwards. The filter used to run over the twenty
                # rows this query returned, so ungraded sittings consumed slots in the
                # window and evicted real exams from it — from the history list AND from
                # `taken`, `passed` and `avg_errors`, all of which are computed from the
                # same twenty. Twenty-one abandoned rows and a learner's entire exam record
                # read as zero. Harmless while abandoning took a deliberate detour through
                # starting another sitting; a live defect the moment Exit became a button.
                QuizSession.passed.is_not(None),
            )
            .order_by(QuizSession.started_at.desc())
            .limit(20)
        )
    )
    graded = rows
    return {
        "taken": len(graded),
        "passed": sum(1 for r in graded if r.passed),
        "avg_errors": round(sum(r.wrong for r in graded) / len(graded), 1) if graded else None,
        # `graded`, not `rows`. The client renders a row as passed only when `passed` is
        # exactly true, so an ungraded sitting would draw a red cross, a "failed" badge and
        # a score of 0/30 — telling the learner they failed an exam they never submitted.
        # Leaving it out is both simpler and more honest than inventing a third badge for
        # something that is not a result. It is still in the event log either way.
        "recent": [
            {
                "id": r.id,
                "finished_at": r.finished_at,
                "wrong": r.wrong,
                "answered": r.answered,
                "question_count": r.question_count,
                "passed": r.passed,
                "state": r.state,
            }
            for r in graded[:5]
        ],
    }


async def user_profile(
    session: AsyncSession, chat_id: int, now: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    readiness, sample = await _readiness(session, chat_id)

    # The streak comes from `streak.refresh`, which counts days the learner MET THE GOAL —
    # ten distinct questions — bridges a single missed day if they have a freeze to spend,
    # and pays out the fourteen-day milestone.
    #
    # There used to be a second, simpler implementation here (`_streak`: consecutive days on
    # which anything at all was answered), kept "to compare against". It is gone. Once the
    # goal became ten questions the two rules gave different answers for the same learner,
    # and a spare copy of a rule that disagrees with the real one is not a safety net — it is
    # the bug, waiting for someone to read the wrong one.
    #
    # Spending happens on read because there is no scheduler here. That is also the right
    # moment: nobody is harmed by a gap nobody has looked at, and it works whenever the
    # learner comes back rather than depending on a job having run in the right timezone.
    user = await session.get(User, chat_id)
    if user is not None:
        streak_days, freezes, _granted = await streak_service.refresh(session, user, now=now)
    else:
        # No user row means there is nothing to spend a freeze against and nobody to grant
        # Premium to, but the days they qualified are still theirs and still count.
        streak_days, freezes = streak_service.count_streak(
            await streak_service.qualifying_days(session, chat_id),
            streak_service.rome_day(now),
            await streak_service.frozen_days(session, chat_id),
        ), 0

    return {
        "streak_days": streak_days,
        "streak_freezes": freezes,
        # Today's progress toward the goal, and the goal itself. Sent rather than hardcoded
        # in the client: the number is a rule of the product, and a second copy of it in the
        # frontend is a copy that disagrees with this one the first time it is tuned.
        "streak_today": await streak_service.counted_today(session, chat_id, now),
        "streak_goal": streak_service.GOAL,
        "readiness": readiness,
        "readiness_sample": sample,
        "readiness_min_sample": MIN_SAMPLE,
        "pass_accuracy": PASS_ACCURACY,
        "exams": await _exams(session, chat_id),
    }
