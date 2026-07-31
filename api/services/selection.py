"""Choosing the next question.

Order of preference:

  1. Something due for review. Oldest due first, so a question missed twenty
     minutes ago comes back before one missed yesterday.
  2. Something never seen. Random, so two users do not walk the bank in the same
     order and a user restarting does not replay the same opening run.
  3. Nothing due and nothing new — the soonest-due question, so the bot always
     has something to serve rather than dead-ending.

`exclude_id` keeps the question just answered from being served straight back:
a wrong answer schedules it ten minutes out, but with a small remaining pool it
could still be the nearest candidate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question
from api.services.entitlement import Entitlement


async def next_question(
    session: AsyncSession,
    user,
    entitlement: Entitlement,
    topic_id: int | None = None,
    exclude_id: int | None = None,
    now: datetime | None = None,
) -> Question | None:
    now = now or datetime.now(timezone.utc)

    def topic_filtered(stmt):
        return stmt.where(Question.topic_id == topic_id) if topic_id else stmt

    def not_excluded(stmt):
        return stmt.where(Question.id != exclude_id) if exclude_id else stmt

    if entitlement.can_use_spaced_repetition:
        due = (
            select(Question)
            .join(Progress, Progress.question_id == Question.id)
            .where(Progress.chat_id == user.chat_id, Progress.due_at <= now)
            .order_by(Progress.due_at)
            .limit(1)
        )
        question = await session.scalar(not_excluded(topic_filtered(due)))
        if question is not None:
            return question

    seen = select(Progress.question_id).where(Progress.chat_id == user.chat_id)
    unseen = (
        select(Question)
        .where(Question.id.not_in(seen))
        .order_by(func.random())
        .limit(1)
    )
    question = await session.scalar(not_excluded(topic_filtered(unseen)))
    if question is not None:
        return question

    if entitlement.can_use_spaced_repetition:
        soonest = (
            select(Question)
            .join(Progress, Progress.question_id == Question.id)
            .where(Progress.chat_id == user.chat_id)
            .order_by(Progress.due_at)
            .limit(1)
        )
        question = await session.scalar(not_excluded(topic_filtered(soonest)))
        if question is not None:
            return question

    # Every question seen and spaced repetition off: just keep drilling.
    anything = select(Question).order_by(func.random()).limit(1)
    return await session.scalar(not_excluded(topic_filtered(anything)))


async def exam_paper(
    session: AsyncSession, count: int, exclude: set[int] | None = None
) -> list[Question]:
    """A whole exam paper in one draw: `count` DISTINCT questions, uniformly random.

    Deliberately NOT built on `next_question`, for two reasons.

    The first is correctness. Calling `next_question` in a loop would re-serve the
    exam's own misses: a wrong answer schedules a question ten minutes out (box 1), the
    exam runs twenty, and the due branch orders strictly by `due_at` — so a question
    missed at minute 2 becomes the top candidate again at minute 12. `exclude_id` blocks
    exactly one previous question and cannot express "none of these thirty".

    The second is what an exam IS. `next_question` is a teaching mechanism: it serves
    what you are weakest at and what is due. A ministerial exam is a uniform random draw
    from the whole bank, and a simulator that quietly tested you on your worst topics
    would report a score that means nothing — it would be systematically pessimistic,
    and the number it produces is the one thing the user is here for.

    ORDER BY random() over 7106 rows is a full scan of a table that is a few megabytes
    and entirely in page cache; measured in milliseconds, and run once per sitting.
    """
    stmt = select(Question)
    if exclude:
        # Practice extends the same sitting rather than starting a new one, so the next
        # batch must not re-serve what this learner has already worked through in it.
        stmt = stmt.where(Question.id.not_in(exclude))
    rows = await session.scalars(stmt.order_by(func.random()).limit(count))
    return list(rows)
