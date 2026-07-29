"""Per-topic error statistics.

Doubles as a product feature and as content validation: a topic where everyone
fails is either genuinely hard or has a broken explanation behind it, and per-topic
error rate is the number that tells them apart (plan §9).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question, Topic


async def user_stats(session: AsyncSession, chat_id: int) -> dict:
    totals = (
        await session.execute(
            select(
                func.count(Progress.question_id),
                func.coalesce(func.sum(Progress.seen), 0),
                func.coalesce(func.sum(Progress.wrong), 0),
            ).where(Progress.chat_id == chat_id)
        )
    ).one()
    questions_seen, answers_given, wrong = totals

    rows = (
        await session.execute(
            select(
                Topic.id,
                Topic.name,
                func.count(Progress.question_id),
                func.coalesce(func.sum(Progress.seen), 0),
                func.coalesce(func.sum(Progress.wrong), 0),
            )
            .join(Question, Question.topic_id == Topic.id)
            .join(
                Progress,
                (Progress.question_id == Question.id) & (Progress.chat_id == chat_id),
            )
            .group_by(Topic.id, Topic.name)
        )
    ).all()

    by_topic = [
        {
            "topic_id": tid,
            "topic": name,
            "questions_seen": seen_q,
            "answers_given": given,
            "wrong": bad,
            "error_rate": round(bad / given, 3) if given else 0.0,
        }
        for tid, name, seen_q, given, bad in rows
    ]
    # Weakest first — this list exists to point at what to study next.
    by_topic.sort(key=lambda t: (-t["error_rate"], -t["answers_given"]))

    boxes = dict(
        (
            await session.execute(
                select(Progress.box, func.count())
                .where(Progress.chat_id == chat_id)
                .group_by(Progress.box)
            )
        ).all()
    )

    total_questions = await session.scalar(select(func.count(Question.id)))

    return {
        "questions_seen": questions_seen,
        "questions_total": total_questions or 0,
        "answers_given": answers_given,
        "wrong": wrong,
        "error_rate": round(wrong / answers_given, 3) if answers_given else 0.0,
        "boxes": {str(b): n for b, n in sorted(boxes.items())},
        "by_topic": by_topic,
    }
