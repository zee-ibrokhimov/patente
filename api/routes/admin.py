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
