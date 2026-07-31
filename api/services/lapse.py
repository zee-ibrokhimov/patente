"""Telling someone their Premium has ended.

Nothing did. A pass reached its date and the features simply stopped: translations
vanished from under the questions, the explanation button started refusing, and the
vocabulary trainer answered 402. No message, no warning, nothing to click.

That is the worst possible moment to say nothing. Someone who has been paying is now
being shown a paywall with no explanation, and their first assumption is that the app
broke rather than that their subscription ended — which is a support message at best and
a refund request at worst. It is also the single moment they are most likely to renew, and
the app was silent through it.

TWO MESSAGES, NOT ONE

  · THREE DAYS BEFORE — worth sending only to someone who CANNOT be auto-renewed, which
    here means a pass with no Tribute subscription behind it. Warning a subscriber that
    their subscription is about to renew is noise; they know, and Tribute tells them.
  · WHEN IT ENDS — for everyone, because the features going quiet needs explaining
    whatever the reason.

IDEMPOTENT BY EVENT, NOT BY COLUMN

Whether someone has already been told is answered from the append-only event log rather
than a "notified" flag. A flag has to be cleared correctly on every renewal, and getting
that wrong means either telling a paying customer their subscription lapsed or never
telling anyone again. The event carries the expiry it was about, so a renewed-then-lapsed
pass is a different event and gets its own message.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, Purchase, User
from api.services import entitlement as entitlement_service
from api.services import events, notify
from shared.constants import EV_PASS_ENDING, EV_PASS_LAPSED

log = logging.getLogger(__name__)

WARN_BEFORE = timedelta(days=3)

# How far back to look for a pass that has just ended. Anything older has either been
# handled or is from before this existed, and messaging someone about a subscription that
# ended a month ago is worse than staying quiet.
LOOK_BACK = timedelta(days=7)


async def _already_told(session: AsyncSession, chat_id: int, kind: str,
                        expiry: datetime) -> bool:
    """Has this exact expiry already been announced?

    Keyed on the DATE as well as the user, so a pass that lapsed, was renewed, and lapsed
    again is announced twice — correctly — while a job running hourly does not repeat
    itself.
    """
    stamp = expiry.isoformat()
    rows = await session.scalars(
        select(Event).where(Event.chat_id == chat_id, Event.type == kind)
    )
    return any((e.payload or {}).get("expires_at") == stamp for e in rows)


def _premium_without_the_pass(user: User) -> bool:
    """Is this person Premium by a route OTHER than the pass column?

    Deliberately not `entitlement.premium`, which includes `has_pass`. The first version
    used that and broke the 3-day warning instantly: someone warned that their pass ends in
    three days HAS an active pass, so `premium` is true for every candidate and the whole
    loop went silent. `test_a_pass_ending_soon_gets_a_warning` caught it.

    "Would they still be Premium once this pass is gone?" is the question both loops
    actually ask, and for the lapsed loop — where the pass has already expired, so
    `has_pass` is false — it gives the same answer as `premium` anyway.

    The pass column is one of three sources. Telling someone their Premium ended because
    that column expired — while channel membership or staff is still granting it, and every
    feature still works — is worse than saying nothing at all: the message is false, it
    points at /plan, and the obvious response is to pay a second time for what they have.

    Exactly the population channel.py was written for. The 2026-07-31 outage answered
    Tribute's webhooks 530 for three hours while buyers were being added to the channel, so
    "their pass row lapsed but they are in the channel" is a state this product has already
    produced once, and it includes the owner.

    `evaluate` is synchronous and reads the cached `channel_status` column — no Telegram
    call, so this is free to run over every candidate.
    """
    ent = entitlement_service.evaluate(user)
    return ent.via_channel or ent.is_staff


async def _has_subscription(session: AsyncSession, chat_id: int) -> bool:
    """Whether money has ever moved for this user.

    Someone with a Tribute subscription will be auto-renewed, so warning them that it is
    about to happen is noise. Someone with a hand-granted pass will not be, so it is not.
    """
    return bool(await session.scalar(
        select(Purchase.id).where(Purchase.chat_id == chat_id,
                                  Purchase.amount_cents > 0).limit(1)
    ))


async def run(session: AsyncSession, now: datetime | None = None) -> dict:
    """Send whatever is due. Returns what was sent, for the caller to log."""
    now = now or datetime.now(timezone.utc)
    warned = lapsed = 0

    # --- about to end -------------------------------------------------------
    soon = await session.scalars(
        select(User).where(
            User.pass_expires_at > now,
            User.pass_expires_at <= now + WARN_BEFORE,
        )
    )
    for user in soon:
        if _premium_without_the_pass(user):
            continue          # channel or staff keeps them Premium; nothing is ending
        if await _has_subscription(session, user.chat_id):
            continue          # Tribute will renew them; saying so would be noise
        if await _already_told(session, user.chat_id, EV_PASS_ENDING, user.pass_expires_at):
            continue
        days = max(1, (user.pass_expires_at - now).days)
        if await notify.payment(user.chat_id, user.lang, "ending",
                                user.pass_expires_at, "", days=days):
            warned += 1
        await events.record(session, EV_PASS_ENDING, chat_id=user.chat_id,
                            expires_at=user.pass_expires_at.isoformat())

    # --- has ended ----------------------------------------------------------
    gone = await session.scalars(
        select(User).where(
            User.pass_expires_at.is_not(None),
            User.pass_expires_at <= now,
            User.pass_expires_at > now - LOOK_BACK,
        )
    )
    for user in gone:
        if _premium_without_the_pass(user):
            # Their pass column expired but Premium did not. No event is recorded either:
            # if they later leave the channel this becomes a real lapse worth announcing.
            continue
        if await _already_told(session, user.chat_id, EV_PASS_LAPSED, user.pass_expires_at):
            continue
        if await notify.payment(user.chat_id, user.lang, "lapsed",
                                user.pass_expires_at, ""):
            lapsed += 1
        await events.record(session, EV_PASS_LAPSED, chat_id=user.chat_id,
                            expires_at=user.pass_expires_at.isoformat())

    await session.commit()
    if warned or lapsed:
        log.info("lapse notices: %s ending, %s ended", warned, lapsed)
    return {"ending": warned, "lapsed": lapsed}
