"""The owner's read-only view.

Loopback only, like the rest of /users/*. nginx proxies exactly two things — /webapp/*
and POST /webhooks/tribute — so /admin/* is unreachable from the internet BY OMISSION,
the same property that lets an unauthenticated API sit behind a public domain at all.
tests/test_webhook_edge.py counts the proxy_pass directives so that stays true.

No auth here for the same reason nothing else in this file has any: the network boundary
IS the authentication. The bot calls it over the internal docker network and gates on
ADMIN_CHAT_IDS before it does.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.services import admin as admin_service

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/overview")
async def overview(session: AsyncSession = Depends(get_session)):
    """Users, entitlement, money and activity — the numbers that say whether this works."""
    return await admin_service.overview(session)


@router.get("/users/{chat_id}")
async def whois(chat_id: int, session: AsyncSession = Depends(get_session)):
    """One person, for answering "I paid and have no access"."""
    found = await admin_service.whois(session, chat_id)
    if found is None:
        raise HTTPException(404, "no such user")
    return found


@router.post("/notify-quiet")
async def notify_quiet(session: AsyncSession = Depends(get_session)):
    """Nudge learners who have stopped opening the app.

    Same trigger as notify-lapses — the hourly cron — and for the same reasons: one process
    per role, no queue, and a background loop inside the API is a long-lived task that has
    to be reasoned about on every deploy.

    Safe to run hourly. The limits live in api/services/reminders.py and are counted from
    the event log, so a run that finds nobody due does nothing and costs one query.
    """
    from api.services import reminders

    return await reminders.run(session)


@router.post("/notify-lapses")
async def notify_lapses(session: AsyncSession = Depends(get_session)):
    """Send "your Premium is ending / has ended" to whoever is due.

    Triggered by cron rather than by a scheduler inside the app: this project has one
    process per role and no queue, and adding a background loop to the API means a
    long-lived task that must be reasoned about on every deploy. A cron entry that POSTs
    here is visible in one file and stops when the box does.

    Idempotent by event, so running it hourly is harmless.
    """
    from api.services import lapse

    return await lapse.run(session)
