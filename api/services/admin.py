"""What the owner needs to see, and what they need to look up.

Two questions this answers, both of which currently require someone with SSH access.

**"How is it going?"** — how many people, how many paying, how many converted from a
trial, how much has been taken. Without this the only reporting is a database the owner
cannot open from their phone.

**"This person says they paid and has no access."** — the one support question a payment
integration guarantees you will get. On 2026-07-31 it would have had a real answer:
webhooks were refused for three hours. Being able to look up one chat id and see their
pass, their channel status and their purchases turns a mystery into a decision.

Everything here is read-only aggregation. The one action — granting a pass — already
exists at POST /users/{chat_id}/pass and is deliberately left there rather than
duplicated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, Purchase, QuizSession, User
from api.services import channel
from shared.constants import (
    EV_PAYWALL_HIT,
    EV_PURCHASE_COMPLETED,
    EV_TRIAL_STARTED,
    MODE_EXAM,
)


async def overview(session: AsyncSession, now: datetime | None = None) -> dict:
    """The numbers that say whether this is working."""
    now = now or datetime.now(timezone.utc)
    day = now - timedelta(days=1)
    week = now - timedelta(days=7)

    users_total = (await session.scalar(select(func.count()).select_from(User))) or 0
    users_week = (await session.scalar(
        select(func.count()).select_from(User).where(User.created_at >= week))) or 0

    # Counted from the same columns entitlement reads, so this cannot disagree with what
    # a user is actually being served.
    with_pass = (await session.scalar(
        select(func.count()).select_from(User).where(User.pass_expires_at > now))) or 0
    in_channel = (await session.scalar(
        select(func.count()).select_from(User)
        .where(User.channel_status.in_(channel.ADMIN_STATUSES + channel.MEMBER_STATUSES)))) or 0

    # Money only — a trial writes a Purchase row at zero so that redelivery stays
    # idempotent, and counting those as revenue would overstate it.
    paid_rows = (await session.execute(
        select(func.count(), func.coalesce(func.sum(Purchase.amount_cents), 0))
        .where(Purchase.amount_cents > 0, Purchase.refunded_at.is_(None))
    )).one()
    refunded = (await session.scalar(
        select(func.count()).select_from(Purchase)
        .where(Purchase.refunded_at.is_not(None)))) or 0

    async def events_since(name: str, since: datetime) -> int:
        return (await session.scalar(
            select(func.count()).select_from(Event)
            .where(Event.type == name, Event.created_at >= since))) or 0

    exams_week = (await session.scalar(
        select(func.count()).select_from(QuizSession)
        .where(QuizSession.mode == MODE_EXAM, QuizSession.started_at >= week))) or 0

    # Where people came from. The whole point of recording it — a count with no
    # breakdown cannot tell the owner which channel to post in again.
    sources = [
        {"source": row[0] or "direct", "users": row[1]}
        for row in (await session.execute(
            select(User.source, func.count())
            .group_by(User.source)
            .order_by(func.count().desc())
            .limit(8)
        )).all()
    ]

    return {
        "sources": sources,
        "users": users_total,
        "users_new_week": users_week,
        "with_pass": with_pass,
        "in_channel": in_channel,
        "purchases": paid_rows[0],
        "revenue_cents": paid_rows[1],
        "refunded": refunded,
        "trials_week": await events_since(EV_TRIAL_STARTED, week),
        "conversions_week": await events_since(EV_PURCHASE_COMPLETED, week),
        "paywall_hits_week": await events_since(EV_PAYWALL_HIT, week),
        "exams_week": exams_week,
        "active_day": (await session.scalar(
            select(func.count(func.distinct(Event.chat_id)))
            .where(Event.created_at >= day))) or 0,
    }


async def whois(session: AsyncSession, chat_id: int, now: datetime | None = None) -> dict | None:
    """Everything about one person, for answering a support message.

    Returns None when there is no such user — which is itself the answer to "I paid and
    have nothing": if they never opened the bot, the webhook created a row, so no row at
    all means the payment never reached us.
    """
    now = now or datetime.now(timezone.utc)
    user = await session.get(User, chat_id)
    if user is None:
        return None

    purchases = (await session.scalars(
        select(Purchase).where(Purchase.chat_id == chat_id).order_by(Purchase.id.desc())
    )).all()
    recent = (await session.scalars(
        select(Event).where(Event.chat_id == chat_id).order_by(Event.id.desc()).limit(5)
    )).all()

    return {
        "chat_id": user.chat_id,
        "lang": user.lang,
        "pass_expires_at": user.pass_expires_at,
        "has_pass": bool(user.pass_expires_at and user.pass_expires_at > now),
        "channel_status": user.channel_status,
        "channel_checked_at": user.channel_checked_at,
        "translations_on": user.translations_on,
        "free_explanations_used": user.free_explanations_used,
        "created_at": user.created_at,
        "purchases": [
            {
                "tribute_id": p.tribute_purchase_id,
                "tier": p.tier,
                "amount_cents": p.amount_cents,
                "extended_to": p.extended_to,
                "refunded_at": p.refunded_at,
            }
            for p in purchases
        ],
        "recent_events": [{"type": e.type, "at": e.created_at} for e in recent],
    }
