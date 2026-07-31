"""The profile: is this person ready, and are they coming back.

Everything here is derived from rows that already exist — `events` for the streak,
`progress` for accuracy, `quiz_sessions` for exam history. Nothing new is stored, which
matters because a "streak" column would need a nightly job to decay it and would be
wrong for anyone in a different timezone than the job.

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

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, Progress, QuizSession
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
# The bar the percentage is measured against: 27 of 30 correct.
PASS_ACCURACY = round((EXAM_QUESTIONS - EXAM_MAX_ERRORS) / EXAM_QUESTIONS, 3)


async def _streak(session: AsyncSession, chat_id: int, today: date) -> int:
    """Consecutive days ending today or yesterday on which the user answered something.

    Ending *or yesterday* on purpose: a streak that breaks at midnight in a timezone the
    server picked is a punishment for the server's convenience. Someone who studied last
    night and opens the app before studying today still has their streak.
    """
    rows = await session.scalars(
        select(func.date(Event.created_at))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)
        .group_by(func.date(Event.created_at))
        .order_by(func.date(Event.created_at).desc())
        .limit(400)
    )
    days = []
    for value in rows:
        if isinstance(value, str):
            value = date.fromisoformat(value[:10])
        days.append(value)
    if not days:
        return 0

    if days[0] < today - timedelta(days=1):
        return 0

    streak = 1
    for previous, current in zip(days, days[1:]):
        if previous - current == timedelta(days=1):
            streak += 1
        else:
            break
    return streak


async def _readiness(session: AsyncSession, chat_id: int) -> tuple[float | None, int]:
    """Accuracy over the most recently touched questions.

    `progress` keeps a per-question running total rather than an answer log, so "the last
    100 answers" is approximated by the 100 most recently answered QUESTIONS. That is the
    honest description of what this measures, and it is close enough for a readiness
    estimate — it just cannot claim to be an exact rolling window.
    """
    rows = (
        await session.execute(
            select(Progress.seen, Progress.wrong)
            .where(Progress.chat_id == chat_id, Progress.last_answer_at.is_not(None))
            .order_by(Progress.last_answer_at.desc())
            .limit(RECENT_WINDOW)
        )
    ).all()

    seen = sum(r[0] or 0 for r in rows)
    wrong = sum(r[1] or 0 for r in rows)
    if seen < MIN_SAMPLE:
        return None, seen
    return round((seen - wrong) / seen, 3), seen


async def _exams(session: AsyncSession, chat_id: int) -> dict:
    rows = list(
        await session.scalars(
            select(QuizSession)
            .where(
                QuizSession.chat_id == chat_id,
                QuizSession.mode == MODE_EXAM,
                QuizSession.state != SESSION_OPEN,
            )
            .order_by(QuizSession.started_at.desc())
            .limit(20)
        )
    )
    graded = [r for r in rows if r.passed is not None]
    return {
        "taken": len(rows),
        "passed": sum(1 for r in graded if r.passed),
        "avg_errors": round(sum(r.wrong for r in graded) / len(graded), 1) if graded else None,
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
            for r in rows[:5]
        ],
    }


async def user_profile(
    session: AsyncSession, chat_id: int, now: datetime | None = None
) -> dict:
    now = now or datetime.now(timezone.utc)
    readiness, sample = await _readiness(session, chat_id)
    return {
        "streak_days": await _streak(session, chat_id, now.date()),
        "readiness": readiness,
        "readiness_sample": sample,
        "readiness_min_sample": MIN_SAMPLE,
        "pass_accuracy": PASS_ACCURACY,
        "exams": await _exams(session, chat_id),
    }
