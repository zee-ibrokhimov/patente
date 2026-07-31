"""Streaks, and the freeze that stops one bad evening erasing a month.

A streak is consecutive days on which the learner answered something. It is computed from
the event log rather than stored, for the reason the profile module already documents: a
"streak" column needs a nightly job to decay it, and that job is wrong for everyone in a
different timezone from the server.

WHY A FREEZE EXISTS

The failure mode of a streak is not that it ends. It is that ending it makes people stop
entirely. Someone thirty days in who misses a Tuesday does not go back to day one — they
conclude the thing they were proud of is gone and quietly stop opening the app. A freeze
covers ONE missed day so that a late shift or a flight does not undo a month.

Deliberately not generous. Two at most, earned one per FREEZE_EVERY days of streak, and
spent automatically without asking. A freeze the learner has to remember to use is a freeze
that fails at exactly the moment it was for, and an unlimited supply is not a streak.

WHICH DAYS WERE COVERED LIVES IN THE EVENT LOG

`users.streak_freezes` is only the balance. The dates are `EV_STREAK_FROZEN` events carrying
the day they covered, which makes the whole thing idempotent: computing the streak twice
cannot spend two freezes, because the second pass finds the day already covered. A "frozen
until" column would have to be cleared correctly on every renewal, and getting that wrong
means either an infinite streak or a freeze that silently does nothing.

SPENT LAZILY, ON READ

There is no scheduler here, so a freeze is applied when the streak is next computed. That is
also when it matters: nobody is harmed by a gap that has not been looked at yet, and doing it
on read means it works whenever the learner returns rather than depending on a job having run
at the right hour in the right timezone.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, User
from api.services import events
from shared.constants import EV_ANSWER_GIVEN, EV_STREAK_FROZEN

log = logging.getLogger(__name__)

# At most this many freezes at once. Two covers a bad week without making the streak
# meaningless — at three or more, "consecutive days" stops describing anything.
MAX_FREEZES = 2

# One freeze per this many days of unbroken streak. Slow on purpose: a freeze should feel
# earned, and someone who has just started has nothing to protect yet.
FREEZE_EVERY = 7

# How far back to look. 400 days of one row per active day, and a streak longer than that is
# not a case worth a slower query.
LOOKBACK_DAYS = 400


def _as_date(value) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        return value.date()
    return value


async def active_days(session: AsyncSession, chat_id: int) -> list[date]:
    """Every day this learner answered something, newest first."""
    rows = await session.scalars(
        select(func.date(Event.created_at))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN)
        .group_by(func.date(Event.created_at))
        .order_by(func.date(Event.created_at).desc())
        .limit(LOOKBACK_DAYS)
    )
    return [_as_date(v) for v in rows]


async def frozen_days(session: AsyncSession, chat_id: int) -> set[date]:
    """Days already covered by a spent freeze."""
    rows = await session.scalars(
        select(Event.payload).where(
            Event.chat_id == chat_id, Event.type == EV_STREAK_FROZEN
        )
    )
    out: set[date] = set()
    for payload in rows:
        day = (payload or {}).get("day")
        if day:
            out.add(_as_date(day))
    return out


def count_streak(days: list[date], today: date, frozen: set[date]) -> int:
    """Consecutive days ending today or yesterday, treating frozen days as covered.

    Pure, so the rule is testable without a database — and the rule is fiddly enough to be
    worth testing directly.

    Ending today OR YESTERDAY on purpose, which predates freezes: a streak that breaks at
    midnight in a timezone the server picked is a punishment for the server's convenience.
    Someone who studied last night and opens the app before studying today still has theirs.
    """
    if not days:
        return 0
    active = set(days)
    if days[0] < today - timedelta(days=1) and days[0] not in frozen:
        # The most recent activity is too old, and nothing bridges the gap to it.
        if not any(d in frozen for d in (today, today - timedelta(days=1))):
            return 0

    streak = 0
    cursor = days[0] if days[0] >= today - timedelta(days=1) else today - timedelta(days=1)
    # Walk back day by day. A day counts if it was active, or if a freeze covered it.
    while cursor >= today - timedelta(days=LOOKBACK_DAYS):
        if cursor in active:
            streak += 1
        elif cursor in frozen:
            pass          # covered, and does NOT add to the count — it was a day off
        else:
            break
        cursor -= timedelta(days=1)
    return streak


def earned(streak: int) -> int:
    """How many freezes a streak of this length has earned, in total, ever."""
    return streak // FREEZE_EVERY


def run_ending_at(days: list[date], anchor: date, frozen: set[date]) -> int:
    """Length of the consecutive run of active days ending at `anchor`.

    Separate from `count_streak` to break a circularity that made the feature useless.

    The balance is DERIVED — earned minus spent — rather than incremented, so it cannot
    drift and cannot be farmed by breaking and restarting. But deriving it from the CURRENT
    streak means that the moment a day is missed the streak is 0, 0 has earned nothing, and
    the learner cannot afford the freeze whose entire purpose is to rescue that exact
    moment. The freeze would only ever be affordable while it was not needed.

    So eligibility is judged on the run they had built up to their last active day. "You
    kept a fourteen-day streak, so you earned two freezes" is true whether or not today has
    already broken it.
    """
    if not days:
        return 0
    active = set(days)
    streak, cursor = 0, anchor
    while cursor >= anchor - timedelta(days=LOOKBACK_DAYS):
        if cursor in active:
            streak += 1
        elif cursor in frozen:
            pass
        else:
            break
        cursor -= timedelta(days=1)
    return streak


async def refresh(
    session: AsyncSession, user: User, today: date | None = None
) -> tuple[int, int]:
    """Bring the streak up to date, spending a freeze if that is what saves it.

    Returns (streak, freezes held). Commits nothing — the caller owns the transaction.
    """
    today = today or datetime.now(timezone.utc).date()
    days = await active_days(session, user.chat_id)
    frozen = await frozen_days(session, user.chat_id)

    if not days:
        user.streak_freezes = 0
        return 0, 0

    def balance(frozen_days: set[date]) -> int:
        """Earned minus spent, capped.

        Judged on the run ending at their LAST ACTIVE DAY, not on the current streak — see
        `run_ending_at`. Derived rather than incremented so it cannot drift out of step with
        the event log, and so a learner cannot farm freezes by breaking and restarting.
        """
        prior = run_ending_at(days, days[0], frozen_days)
        return min(MAX_FREEZES, max(0, earned(prior) - len(frozen_days)))

    # A freeze is spent for exactly one missing day: yesterday, when the learner was active
    # the day before. Only yesterday — a longer absence is not a slip, and bridging it would
    # make the number a lie, which is worse than losing the streak.
    gap = today - timedelta(days=1)
    active = set(days)
    if (
        gap not in active
        and gap not in frozen
        and today not in active
        and (gap - timedelta(days=1)) in active
        and balance(frozen) > 0
    ):
        frozen.add(gap)
        await events.record(session, EV_STREAK_FROZEN, chat_id=user.chat_id,
                            day=gap.isoformat())
        log.info("spent a streak freeze for %s to cover %s", user.chat_id, gap)

    streak = count_streak(days, today, frozen)
    # Recomputed AFTER the spend, from the event log — so the freeze just recorded is
    # counted as spent and the balance actually goes down. The first version recomputed the
    # allowance and handed the same freeze straight back, which a test caught.
    user.streak_freezes = balance(frozen)
    return streak, user.streak_freezes
