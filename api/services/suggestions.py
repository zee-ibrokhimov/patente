"""What the learners think is missing.

The first version of this was a link to the support chat. It asked somebody to compose a
message to a stranger, which almost nobody does, and it put "add a dark mode" in the same
inbox as "my payment failed". A form asks for one thing, in the language the app is already
speaking, and lands in a list.

WHY THERE IS A LIMIT AND WHY IT IS GENTLE
This is the one endpoint in the product that stores free text a user typed. Unbounded, it is
a place to paste a novel, and enough of those fill a disk. But a learner with three good
ideas in one evening is exactly who this is for, so the cap is per day and generous, and the
refusal says which rule was hit rather than failing silently.

WHAT IS NOT STORED
Nothing about the message beyond its text, its language and who sent it — no screen, no
device, no session. The owner needs to be able to reply and to know which language to reply
in; everything else would be collected because it was easy rather than because it was
needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Suggestion

# Long enough for a real thought, short enough that the column is not a document store.
# Measured against the shape of the ask: "add a dark mode", "the vocabulary needs audio",
# "let me choose which topics to practise" are all under 200 characters.
MAX_LENGTH = 1_000

# Per rolling day. A learner with three ideas in an evening is the point of the feature; a
# script pasting for an hour is not.
DAILY_LIMIT = 10


class Refused(Exception):
    """The message was empty, too long, or one too many today."""


async def submit(
    session: AsyncSession, chat_id: int, text: str, lang: str,
    now: datetime | None = None,
) -> Suggestion:
    now = now or datetime.now(timezone.utc)

    cleaned = (text or "").strip()
    if not cleaned:
        raise Refused("write something first")
    if len(cleaned) > MAX_LENGTH:
        # Refused rather than truncated. Silently cutting somebody's last sentence off is
        # worse than telling them, and they cannot tell it happened.
        raise Refused(f"keep it under {MAX_LENGTH} characters")

    since = now - timedelta(days=1)
    today = await session.scalar(
        select(func.count(Suggestion.id))
        .where(Suggestion.chat_id == chat_id, Suggestion.created_at >= since)
    ) or 0
    if today >= DAILY_LIMIT:
        raise Refused("that is enough for today — thank you, we have read them")

    row = Suggestion(chat_id=chat_id, text=cleaned, lang=lang, created_at=now)
    session.add(row)
    await session.flush()
    return row


async def queue(session: AsyncSession, limit: int = 100) -> dict:
    """Unhandled first, newest first — the order the owner actually reads in."""
    rows = list(await session.scalars(
        select(Suggestion)
        .order_by(Suggestion.handled_at.is_not(None), Suggestion.created_at.desc())
        .limit(limit)
    ))
    open_count = await session.scalar(
        select(func.count(Suggestion.id)).where(Suggestion.handled_at.is_(None))) or 0
    return {
        "suggestions": [
            {
                "id": r.id,
                "chat_id": r.chat_id,
                "text": r.text,
                "lang": r.lang,
                "created_at": r.created_at,
                "handled": r.handled_at is not None,
            }
            for r in rows
        ],
        "open": open_count,
    }


async def mark_handled(session: AsyncSession, suggestion_id: int,
                       now: datetime | None = None) -> bool:
    row = await session.get(Suggestion, suggestion_id)
    if row is None:
        return False
    row.handled_at = now or datetime.now(timezone.utc)
    await session.flush()
    return True
