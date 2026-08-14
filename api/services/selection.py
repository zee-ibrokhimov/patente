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
from sqlalchemy import true as sa_true
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question
from api.services.entitlement import Entitlement
from shared.constants import REPEAT_WRONG


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


async def practice_paper(
    session: AsyncSession,
    user,
    entitlement: Entitlement,
    count: int,
    exclude: set[int] | None = None,
    now: datetime | None = None,
    topic_ids: set[int] | None = None,
) -> list[Question]:
    """A practice batch that serves what this learner is about to forget.

    THE BUG THIS FIXES. Practice was drawing from `exam_paper` — ORDER BY random() over
    all 7106 questions. Every answer wrote a Leitner box and a due date into `progress`,
    and NOTHING ever read them back: the only reader is `next_question`, reachable solely
    from the loopback route the bot used before quizzing moved into the Mini App. So a
    learner answered a question wrong, it was scheduled to return in ten minutes, and it
    never came. The app recorded spaced repetition and practised uniform random.

    Order, and each part earns its place:

      1. DUE — what they got wrong, soonest first. This is the whole point.
      2. UNSEEN — new material, at random.
      3. NOT-YET-DUE — rather than an empty paper, because a short session that runs out
         is worse than one that revises slightly early.

    The exam keeps its uniform random draw. A simulator that quietly tested you on your
    weakest topics would report a score that means nothing — see exam_paper.
    """
    now = now or datetime.now(timezone.utc)
    exclude = exclude or set()
    picked: list[Question] = []
    seen_ids = set(exclude)

    # Narrowing the draw to one subject, when the learner asked for one. Applied to ALL
    # THREE tiers rather than only to the first: a filter on the due tier alone would serve
    # their overdue signs questions and then quietly pad the sitting out with rules, which
    # is the feature appearing to work and not working.
    def in_scope(stmt):
        return stmt.where(Question.topic_id.in_(topic_ids)) if topic_ids else stmt

    def take(rows):
        for q in rows:
            if q.id in seen_ids or len(picked) >= count:
                continue
            seen_ids.add(q.id)
            picked.append(q)

    if entitlement.can_use_spaced_repetition:
        due = await session.scalars(
            in_scope(
                select(Question)
                .join(Progress, Progress.question_id == Question.id)
                .where(Progress.chat_id == user.chat_id, Progress.due_at <= now,
                       Question.id.not_in(exclude) if exclude else sa_true())
            )
            .order_by(Progress.due_at)
            .limit(count)
        )
        take(due)

    if len(picked) < count:
        already = select(Progress.question_id).where(Progress.chat_id == user.chat_id)
        fresh = await session.scalars(
            in_scope(
                select(Question)
                .where(Question.id.not_in(already))
                .where(Question.id.not_in(seen_ids) if seen_ids else sa_true())
            )
            .order_by(func.random())
            .limit(count - len(picked))
        )
        take(fresh)

    if len(picked) < count:
        # Everything seen and nothing due yet. Revising early beats stopping.
        soon = await session.scalars(
            in_scope(
                select(Question)
                .join(Progress, Progress.question_id == Question.id)
                .where(Progress.chat_id == user.chat_id)
                .where(Question.id.not_in(seen_ids) if seen_ids else sa_true())
            )
            .order_by(Progress.due_at)
            .limit(count - len(picked))
        )
        take(soon)

    return picked


async def repeat_paper(
    session: AsyncSession,
    user,
    kind: str,
    count: int,
    exclude: set[int] | None = None,
) -> list[Question]:
    """Questions the learner has answered before — the ones they got wrong, or the ones
    they got right.

    Asked for directly by the owner: "can repeat correct answered questions and also
    uncorrect ones? cuz this is important".

    `practice_paper` already resurfaces mistakes, but it does it on the Leitner schedule —
    when the algorithm judges you are about to forget, mixed in with new material, and never
    on demand. That is the right default and the wrong tool for "I have my test on Friday,
    show me everything I have ever got wrong". Someone revising to a deadline wants a
    deliberate pass over a known set, and someone who wants reassurance wants the opposite.

    WRONG means `wrong > 0`: ever missed, not currently-missed. A question got wrong twice
    and right once is still a question they have demonstrably struggled with, and dropping
    it the moment it goes right would make this mode quietly forget the material it exists
    to drill.

    RIGHT means seen, and never wrong. Deliberately strict: a learner asking to review what
    they know is checking that it is still solid, so anything with a mistake against it
    belongs in the other list rather than in both.

    Ordered by `last_answer_at` OLDEST FIRST — the thing they have not looked at for longest
    is the thing most likely to have gone stale, and it makes repeated rounds walk through
    the set instead of re-serving the same head every time.

    Untouched by all of this: `progress`. A repeat round records answers exactly as practice
    does, because it IS practice — the learner is studying and the schedule should learn
    from it. Only the DRAW is different.
    """
    exclude = exclude or set()
    condition = (
        Progress.wrong > 0 if kind == REPEAT_WRONG
        else Progress.wrong == 0
    )
    stmt = (
        select(Question)
        .join(Progress, Progress.question_id == Question.id)
        .where(
            Progress.chat_id == user.chat_id,
            Progress.seen > 0,
            condition,
        )
        .order_by(Progress.last_answer_at.asc().nulls_last())
        .limit(count)
    )
    if exclude:
        stmt = stmt.where(Question.id.not_in(exclude))
    return list(await session.scalars(stmt))
