"""Append-only event log (plan §9).

Instrumented from the first commit because none of it can be backfilled. Every
number reported later — conversion, wrong-answers-before-purchase, per-topic
error rate, report volume per 1,000 served — is derived from these rows rather
than counted separately, so there is one definition of each and no drift.

Logging never raises into the caller's path: a failed analytics insert must not
cost a user their answer.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event

log = logging.getLogger(__name__)


async def record(
    session: AsyncSession,
    event_type: str,
    chat_id: int | None = None,
    **payload,
) -> None:
    """Queue an event on the caller's transaction. Committed with their work."""
    try:
        session.add(
            Event(
                chat_id=chat_id,
                type=event_type,
                payload=payload or None,
            )
        )
        await session.flush()
    except Exception:  # pragma: no cover - analytics must never break the request
        log.exception("failed to record event %s for %s", event_type, chat_id)
