"""Where a learner is losing marks, and how many they would lose today.

This exists because the number the app already shows is not actionable. Two problems, and
the second is the one that matters.

THE HEADLINE WAS MEASURED OVER ALL TIME
`stats.user_stats` divides lifetime wrong by lifetime answers. After a few hundred answers
that figure barely moves, so it stops rewarding improvement at exactly the point improvement
begins — a learner who was at 40% a month ago and is at 12% today reads 31% and concludes
they have not moved. The window here is the last 100 answers, which is about 3.3 exams and
is the same window `profile._readiness` uses. Two screens quoting accuracy over two
different windows contradict each other and the learner cannot tell which to believe.

THE LIST WAS RANKED BY ERROR RATE, WHICH IS THE WRONG ORDER
A topic where somebody answered 3 questions and missed 2 sits at 67% and sorts above one
where they answered 300 and missed 120 at 40%. The first is noise; the second is where every
lost mark actually lives. So the ranking here is **expected mistakes on a 30-question exam**
— a family's error rate multiplied by how much of the exam that family is. That number is
computable rather than estimated, because the exam draws uniformly from the bank, so a
family's share of the bank IS its share of the paper.

It also gives the screen one sentence a learner can act on: *"On a 30-question exam you
would average 4.2 mistakes. You pass at 3."*

WHY THE TOPIC WINDOW IS 90 DAYS AND COUNTS EACH QUESTION ONCE
The shipped per-topic figure is a lifetime tally of `Progress.seen` and `Progress.wrong`, so
a question missed four times in March and answered correctly ever since drags its topic down
permanently. There is no action the learner can take that clears it, which makes the whole
list advice they cannot follow. Counting the most recent answer per question, within 90
days, means fixing something actually fixes the number.

AN HONEST CAVEAT THAT BELONGS IN THE PAYLOAD, NOT IN A FOOTNOTE
Practice deliberately re-serves what you got wrong. So a learner's measured error rate on
the questions they have met is PESSIMISTIC compared to a fresh exam drawn from the whole
bank, and `predicted_mistakes` is an upper bound on the questions they have seen — never a
plain prediction of exam day. `coverage` is returned on every row so the client can say so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, Question
from shared.constants import (
    ERROR_MIN_SAMPLE,
    ERROR_WINDOW,
    EV_ANSWER_GIVEN,
    EXAM_QUESTIONS,
    FAMILY_OF_TOPIC,
    TOPIC_FAMILIES,
    TOPIC_MIN_SAMPLE,
    TOPIC_WINDOW_DAYS,
)

# The real exam tolerates 3 errors in 30. Reported alongside the prediction so the number
# has something to mean.
EXAM_MAX_ERRORS = 3


async def _recent_answers(session: AsyncSession, chat_id: int, limit: int) -> list[dict]:
    """The learner's last `limit` answer events, newest first.

    Read from the EVENT LOG rather than from `Progress`, because progress is a running
    tally with no history: it can say a learner has missed a question four times but not
    whether that was this week or in March, and every window in this module needs to know.
    """
    rows = list(await session.scalars(
        select(Event.payload)
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)
        .order_by(Event.created_at.desc(), Event.id.desc())
        .limit(limit)
    ))
    return [r for r in rows if isinstance(r, dict)]


async def headline(session: AsyncSession, chat_id: int, now: datetime | None = None) -> dict:
    """The error rate over the last ERROR_WINDOW answers, or a refusal to state one.

    `rate` is None below ERROR_MIN_SAMPLE. That is not a placeholder for zero — it is the
    screen declining to put a percentage in front of somebody deciding whether to book a
    paid exam on the strength of twelve answers.
    """
    recent = await _recent_answers(session, chat_id, ERROR_WINDOW)
    sample = len(recent)
    wrong = sum(1 for p in recent if p.get("correct") is False)

    lifetime_total = await session.scalar(
        select(func.count(Event.id))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)) or 0

    return {
        "rate": round(wrong / sample, 3) if sample >= ERROR_MIN_SAMPLE else None,
        "sample": sample,
        "min_sample": ERROR_MIN_SAMPLE,
        # All-time, kept as a caption rather than as the headline. It answers a different
        # question — "how far have I come" — and it is honest as long as it is not the
        # number being watched.
        "lifetime_answers": lifetime_total,
    }


async def families(
    session: AsyncSession,
    chat_id: int,
    now: datetime | None = None,
    *,
    min_sample: int = TOPIC_MIN_SAMPLE,
) -> list[dict]:
    """One row per family: how often it appears on an exam, how the learner does on it, and
    therefore how many marks it is costing them.

    Sorted by `predicted_mistakes` — the point of the whole screen. Ranking by error rate
    puts noise at the top; ranking by lost marks puts the biggest win at the top.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=TOPIC_WINDOW_DAYS)

    # Size of each family in the bank. An exam draws uniformly, so share-of-bank is
    # share-of-exam, which is what turns an error rate into a number of marks.
    sizes = dict(
        (await session.execute(
            select(Question.topic_id, func.count(Question.id)).group_by(Question.topic_id)
        )).all()
    )
    bank = sum(sizes.values()) or 1

    # The most recent answer per QUESTION inside the window. Newest first, so the first
    # time a question id is seen is its latest answer and the rest are history.
    rows = list(await session.scalars(
        select(Event.payload)
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN,
               Event.created_at >= since)
        .order_by(Event.created_at.desc(), Event.id.desc())
    ))

    latest: dict[int, bool] = {}
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        qid = payload.get("question_id")
        if qid is None or qid in latest:
            continue
        latest[qid] = payload.get("correct") is not False

    # Which family each answered question belongs to. One query rather than one per
    # question; the id list is bounded by what the learner has actually answered.
    topics: dict[int, int] = {}
    if latest:
        topics = dict((await session.execute(
            select(Question.id, Question.topic_id).where(Question.id.in_(list(latest)))
        )).all())

    out = []
    for family, topic_ids in TOPIC_FAMILIES.items():
        in_bank = sum(sizes.get(t, 0) for t in topic_ids)
        share = in_bank / bank

        answered = [ok for qid, ok in latest.items()
                    if FAMILY_OF_TOPIC.get(topics.get(qid, -1)) == family]
        seen = len(answered)
        wrong = sum(1 for ok in answered if not ok)
        # Gated on the sample, not merely on "did they answer anything". Computing the rate
        # whenever seen > 0 and only hiding it from `error_rate` left `predicted_mistakes`
        # reading from an ungated value: one answered question, one mistake, and the screen
        # announced fifteen mistakes per exam. Both numbers come from the same gate now.
        enough = seen >= min_sample
        rate = (wrong / seen) if enough else None

        out.append({
            "family": family,
            "questions_in_bank": in_bank,
            "share": round(share, 4),
            # What this family contributes to a 30-question paper.
            "per_exam": round(share * EXAM_QUESTIONS, 1),
            "answered": seen,
            "wrong": wrong,
            # None, not zero, below the threshold: under ten questions the margin of error
            # is wider than the whole useful range of the number.
            "error_rate": round(rate, 3) if rate is not None else None,
            "enough": enough,
            # How much of this family the learner has ever met. 0% errors on 12 of 662
            # information-sign questions is not mastery, and without this the ranking would
            # present it as their strongest area.
            "coverage": round(seen / in_bank, 4) if in_bank else 0.0,
            # The marks this family is costing, on the questions they have met.
            "predicted_mistakes": round(rate * share * EXAM_QUESTIONS, 2)
            if rate is not None else None,
        })

    # Untested families sort last rather than first: `None` is "we do not know", and a
    # screen that opens by pointing at what it cannot measure is pointing at nothing.
    out.sort(key=lambda f: (f["predicted_mistakes"] is None,
                            -(f["predicted_mistakes"] or 0)))
    return out


async def report(
    session: AsyncSession,
    chat_id: int,
    now: datetime | None = None,
    *,
    min_sample: int = TOPIC_MIN_SAMPLE,
) -> dict:
    """Everything the breakdown screen needs, in one round trip.

    `min_sample` is a parameter rather than only a module constant because the seeded test
    bank is four questions and the real gate is ten DISTINCT ones — unreachable there. It was
    briefly monkeypatched instead, which produced a test that passed alone and failed after
    any other test in the file, for reasons that took longer to chase than threading one
    argument through. A default argument is the same rule with none of that.
    """
    rows = await families(session, chat_id, now, min_sample=min_sample)
    measured = [f for f in rows if f["predicted_mistakes"] is not None]

    # Only over the families we can actually speak to. Summing across untested ones as if
    # they were zero would report a flattering total that improves as the learner AVOIDS
    # material — the one direction a study metric must never move.
    total = round(sum(f["predicted_mistakes"] for f in measured), 1) if measured else None
    covered = round(sum(f["share"] for f in measured), 3) if measured else 0.0

    return {
        "headline": await headline(session, chat_id, now),
        "families": rows,
        "predicted_mistakes": total,
        # What fraction of an exam that prediction actually speaks for. A learner who has
        # only ever answered sign questions has a prediction covering 34% of the paper, and
        # the screen has to say so rather than implying it covers all of it.
        "predicted_covers": covered,
        "exam_questions": EXAM_QUESTIONS,
        "exam_max_errors": EXAM_MAX_ERRORS,
    }
