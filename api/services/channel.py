"""Who is in the Premium channel, and what that entitles them to.

Tribute sells a subscription attached to a Telegram channel and adds buyers to it itself.
That makes channel membership a SECOND, independent record of who has paid — one that
lives on Telegram's servers rather than in our database.

WHY THAT IS WORTH HAVING

On 2026-07-31 this app was unreachable for three hours. Tribute's webhooks were answered
with 530 by Cloudflare the whole time. Anyone who paid during that window would have had
no pass here — while sitting in the channel Tribute had just added them to.

So membership is treated as an ADDITIONAL grant, never as a requirement. Requiring both a
pass and membership would have cut off exactly the people the outage had already failed.
Nobody can be in that channel without either paying or being added by the owner, so there
is nothing to game.

ADMINS ARE PREMIUM, FULL STOP

An administrator or the creator of the channel gets Premium regardless of any payment.
They are running the thing; asking them to subscribe to their own product is absurd, and
it is how the owner tests paid features without paying themselves.

CACHED, BECAUSE TELEGRAM IS NOT FREE
Every check is an API call. The answer changes rarely — people do not join and leave a
paid channel minute to minute — so it is stored on the user row and refreshed on a TTL.
A stale answer is always preferred to a slow request or a rate limit.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from shared.config import settings

log = logging.getLogger(__name__)

# How long a cached answer is trusted. Fifteen minutes is long enough that a normal
# session never triggers a call, and short enough that someone who has just been added
# by Tribute is not left waiting for what they paid for.
TTL = timedelta(minutes=15)

TIMEOUT = 8.0

# Statuses Telegram can return from getChatMember.
ADMIN_STATUSES = ("creator", "administrator")
MEMBER_STATUSES = ("member", "restricted")
GONE_STATUSES = ("left", "kicked")

# Set when a check has never succeeded. Deliberately NOT "left": never having asked is
# not the same as having asked and been told no, and only one of those should withhold
# access from someone who may have paid.
UNKNOWN = "unknown"


def grants_premium(status: str | None) -> bool:
    """Whether this membership status is worth Premium on its own."""
    return status in ADMIN_STATUSES + MEMBER_STATUSES


def is_admin(status: str | None) -> bool:
    return status in ADMIN_STATUSES


def is_stale(checked_at: datetime | None, now: datetime | None = None) -> bool:
    if checked_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return now - checked_at > TTL


async def fetch_status(chat_id: int) -> str | None:
    """Ask Telegram. Returns None when the question could not be asked at all.

    None and "left" are different answers and are treated differently by the caller: a
    failed call must never downgrade someone who was previously a member, because a
    Telegram blip would then revoke Premium from paying customers en masse.
    """
    token = settings.bot_token
    if not token or not settings.premium_channel_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getChatMember",
                params={"chat_id": settings.premium_channel_id, "user_id": chat_id},
            )
        body = r.json()
        if not body.get("ok"):
            description = str(body.get("description", ""))
            # "user not found" means they have never been in the channel — a real answer,
            # not a failure, and the only error worth recording as one.
            if "not found" in description.lower():
                return "left"
            log.warning("channel check for %s failed: %s", chat_id, description[:160])
            return None
        return str(body["result"]["status"])
    except Exception as exc:  # noqa: BLE001 — never break a request over this
        log.warning("channel check for %s failed: %s", chat_id, exc)
        return None


async def refresh(session, user, *, force: bool = False, now: datetime | None = None) -> str | None:
    """Update the cached status if it is stale. Returns the status in effect.

    Commits nothing: the caller owns the transaction. A failed lookup leaves the previous
    answer untouched and does NOT reset the timer, so the next request tries again rather
    than trusting a value we could not confirm.
    """
    now = now or datetime.now(timezone.utc)
    if not force and not is_stale(user.channel_checked_at, now):
        return user.channel_status

    status = await fetch_status(user.chat_id)
    if status is None:
        return user.channel_status

    if status != user.channel_status:
        log.info("channel status for %s: %s -> %s", user.chat_id, user.channel_status, status)
    user.channel_status = status
    user.channel_checked_at = now
    return status
