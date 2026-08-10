"""One message a day, to the people about to lose a streak they have actually built.

The streak is now worth something — every fourteenth day pays three days of Premium — and
the way people lose one is not deciding to stop. It is getting to nine in the evening
without realising the day is not banked yet. That is the entire case for this message, and
it is also the boundary of it: it is a reminder about work in progress, not a re-engagement
campaign. `reminders.py` already owns bringing back somebody who has gone quiet.

THIS IS THE SECOND UNSOLICITED MESSAGE THE PRODUCT SENDS, AND THE COST OF GETTING IT WRONG
IS PRECISE

A learner who mutes the bot over a streak nudge also stops receiving "your Premium ends
Friday" — the message the business runs on. So the conditions are narrow and every one of
them is a refusal:

  · a streak of NEEDS_STREAK or more. Nobody is nagged about a streak they have not
    invested in yet, and at one or two days the message is just noise about a habit that
    does not exist.
  · today's goal NOT yet met. The whole point is the gap between "I studied" and "the day
    counts"; someone who has already done their ten has nothing to be warned about.
  · the evening, in Rome. Sent in the window below, so it lands while there is still time
    to act on it. A morning nudge is a nag; a 23:30 nudge is a taunt.
  · once per Rome day, from the event log, so an hourly cron cannot send it twice.
  · `reminders_off` — the same explicit stop as every other message.

NOT SENT TO SOMEBODY WHOSE STREAK A FREEZE WOULD SAVE ANYWAY? IT IS.

Deliberately. A freeze is spent automatically and silently, and someone who would rather
keep the freeze than spend it can only do that by studying — which is what the message asks
for. Suppressing it would mean the product quietly spends a freeze it could have avoided.

WINDOW, NOT AN INSTANT

The cron is hourly and can be late, and a job that only fires at exactly 19:00 Rome misses
the day entirely if that run is skipped. An hour of slack costs nothing and removes a whole
class of silent non-delivery.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, User
from api.services import events, notify, streak
from shared.constants import EV_STREAK_NUDGE

log = logging.getLogger(__name__)

# Rome hours during which the message may go out. Evening, while the day can still be saved.
SEND_FROM = 19
SEND_UNTIL = 22

# Streak length below which nobody is nudged.
NEEDS_STREAK = 3

# Blast radius, not throughput. A bug that made everyone look at risk would otherwise
# message the whole user base before anybody noticed — the same reasoning as reminders.py.
MAX_PER_RUN = 50


def in_window(now: datetime) -> bool:
    return SEND_FROM <= now.astimezone(streak.ROME).hour < SEND_UNTIL


async def nudged_today(session: AsyncSession, chat_id: int, today) -> bool:
    """Read from the event log, not a column: an hourly job needs an idempotency key that
    cannot drift, and "did we message this person today" is a question the log answers
    directly."""
    rows = await session.scalars(
        select(Event.payload).where(
            Event.chat_id == chat_id, Event.type == EV_STREAK_NUDGE)
    )
    stamp = today.isoformat()
    return any((p or {}).get("day") == stamp for p in rows)


async def due(session: AsyncSession, now: datetime | None = None) -> list[tuple[User, int]]:
    """Everyone at risk right now, with the streak they stand to lose."""
    now = now or datetime.now(timezone.utc)
    if not in_window(now):
        return []

    today = streak.rome_day(now)
    frozen_out: list[tuple[User, int]] = []

    # Only people with a qualifying day on the books at all — which is a small table, and
    # excludes everybody who has never met the goal without loading them.
    chat_ids = list(await session.scalars(
        select(User.chat_id)
        .where(User.reminders_off.is_(False))
        .order_by(User.chat_id)
    ))

    for chat_id in chat_ids:
        days = await streak.qualifying_days(session, chat_id)
        if not days or days[0] == today:
            continue                      # never started, or already banked today
        frozen = await streak.frozen_days(session, chat_id)
        run = streak.current_run(days, today, frozen)
        if len(run) < NEEDS_STREAK:
            continue
        if await nudged_today(session, chat_id, today):
            continue
        user = await session.get(User, chat_id)
        if user is None:
            continue
        frozen_out.append((user, len(run)))
        if len(frozen_out) >= MAX_PER_RUN:
            break
    return frozen_out


async def run(session: AsyncSession, now: datetime | None = None) -> dict:
    """Send what is due. Returns counts, for the caller to log."""
    now = now or datetime.now(timezone.utc)
    if not in_window(now):
        return {"due": 0, "delivered": 0, "window": False}

    today = streak.rome_day(now)
    people = await due(session, now)

    sent = 0
    for user, days in people:
        left = max(0, streak.GOAL - await streak.counted_today(session, user.chat_id, now))
        # Recorded whether or not the send succeeded, for the reason reminders.py gives: a
        # blocked bot fails every time, and retrying it hourly would put that person at the
        # front of the queue for ever. The record is of the ATTEMPT.
        ok = await notify.streak_at_risk(user.chat_id, user.lang, days=days, left=left)
        sent += 1 if ok else 0
        await events.record(session, EV_STREAK_NUDGE, chat_id=user.chat_id,
                            day=today.isoformat(), streak=days, delivered=bool(ok))

    await session.commit()
    if people:
        log.info("streak nudges: %s due, %s delivered", len(people), sent)
    return {"due": len(people), "delivered": sent, "window": True}
