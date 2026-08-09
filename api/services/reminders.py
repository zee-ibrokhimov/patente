"""Bringing back a learner who has simply stopped opening the app.

Asked for as "notifications for my users when they are not using my app". Nothing in the
product reached them: `lapse.py` messages on a DATE in `users.pass_expires_at`, never on
silence, so somebody who drifted away with two months left on their pass heard nothing at
all — and drifting away is what almost everyone does.

WHAT MAKES THIS DIFFERENT FROM SPAM IS ENTIRELY IN THE LIMITS

A nudge is welcome once. The same nudge every week is why people mute bots, and a muted bot
cannot deliver the renewal warning either — so an over-eager reminder costs the messages
that actually matter.

  · QUIET_AFTER  — 10 days. Long enough that a busy week is not "gone", short enough to
    arrive while the exam still feels near.
  · MIN_GAP      — 21 days between reminders for one person.
  · MAX_EVER     — 3, then never again. Someone who has ignored three is not undecided;
    they have finished with it, and the honest reading of silence is that they left.
  · NEEDS_HISTORY — 10 answers. A person who opened the app once and never returned did
    not lapse, they bounced, and a "come back" message to somebody who never arrived reads
    as a bot that noticed them leaving. It also excludes the accidental /start.
  · reminders_off — an explicit stop, on every message.

IDEMPOTENT BY EVENT, like lapse.py

Whether somebody has been reminded, and how often, is read from the append-only event log
rather than a counter column. A counter has to be reset correctly on return and getting
that wrong means either nagging someone for ever or never reminding anyone again. The log
also answers "how many did we send in March", which a counter cannot.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, User
from api.services import events, notify
from shared.constants import EV_ANSWER_GIVEN, EV_REMINDER_SENT, USER_ACTIVITY_EVENTS

log = logging.getLogger(__name__)

QUIET_AFTER = timedelta(days=10)
MIN_GAP = timedelta(days=21)
MAX_EVER = 3
NEEDS_HISTORY = 10

# Never message more than this in one run. The cron is hourly and the population is small,
# so this is not a throughput limit — it is a blast radius. A bug in the query that made
# everybody look quiet would otherwise message the entire user base before anyone noticed.
MAX_PER_RUN = 50


async def _reminder_history(session: AsyncSession, chat_id: int) -> tuple[int, datetime | None]:
    """(how many reminders they have had, when the last one was)."""
    rows = list(await session.scalars(
        select(Event).where(Event.chat_id == chat_id, Event.type == EV_REMINDER_SENT)
        .order_by(Event.created_at.desc())
    ))
    return len(rows), (rows[0].created_at if rows else None)


async def due(session: AsyncSession, now: datetime | None = None) -> list[User]:
    """Everyone worth nudging right now, in a stable order.

    Quiet is measured from the USER'S OWN events, not every row with their chat_id on it.
    The log holds both kinds, and counting the system's writes is circular: the reminder
    itself lands in the log, so a nudged learner looked active for the next ten days and
    became permanently ineligible. The MIN_GAP check below was unreachable — removing it
    changed no test, which is how this was found.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - QUIET_AFTER

    seen_recently = select(func.distinct(Event.chat_id)).where(
        Event.created_at > cutoff, Event.type.in_(USER_ACTIVITY_EVENTS))
    candidates = list(await session.scalars(
        select(User)
        .where(User.reminders_off.is_(False), User.chat_id.not_in(seen_recently))
        .order_by(User.chat_id)
    ))

    out: list[User] = []
    for user in candidates:
        answered = await session.scalar(
            select(func.count(Event.id)).where(
                Event.chat_id == user.chat_id, Event.type == EV_ANSWER_GIVEN))
        if (answered or 0) < NEEDS_HISTORY:
            continue                     # they bounced; they did not lapse
        sent, last = await _reminder_history(session, user.chat_id)
        if sent >= MAX_EVER:
            continue                     # three ignored is an answer
        if last and now - last < MIN_GAP:
            continue
        out.append(user)
        if len(out) >= MAX_PER_RUN:
            break
    return out


async def run(session: AsyncSession, now: datetime | None = None) -> dict:
    """Send what is due. Returns counts, for the caller to log."""
    now = now or datetime.now(timezone.utc)
    people = await due(session, now)

    sent = 0
    for user in people:
        # Recorded whether or not the send succeeded. A blocked bot returns False every
        # time, and retrying it hourly for ever would keep this person permanently at the
        # front of the queue — the record is of the ATTEMPT, which is what the limits are
        # really about.
        ok = await notify.reminder(user.chat_id, user.lang)
        sent += 1 if ok else 0
        await events.record(session, EV_REMINDER_SENT, chat_id=user.chat_id,
                            delivered=bool(ok))

    await session.commit()
    if people:
        log.info("reminders: %s due, %s delivered", len(people), sent)
    return {"due": len(people), "delivered": sent}
