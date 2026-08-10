"""Streaks: what a day costs, and the freeze that stops one bad evening erasing a month.

A streak is consecutive days on which the learner MET THE DAILY GOAL. Until recently the
goal was "answered something", which one tap satisfied. It is now ten distinct questions.

WHY TEN DISTINCT QUESTIONS, RIGHT OR WRONG

Not "ten correct". Measured accuracy across the whole event log is 55%, so ten correct is
eighteen answers for an average learner and closer to forty for a struggling one — the
person who needs the habit most would pay double for it. Worse, it contradicts the method:
this app teaches by spaced repetition, where a WRONG answer is the thing that schedules the
review. It is worth more than a right one. Performance belongs in readiness and in the
league. The streak measures showing up.

Not escalating, either. A requirement that rises against time that does not rise guarantees
its own end; it is a countdown, not a motivator. It would also exhaust the material — a
thirty-a-day goal walks all 7,106 questions in 229 days and then serves nothing but repeats.

DISTINCT, and this is not pedantry: the scheduler re-serves a missed question after ten
minutes, so counting repeats would make the goal a sixty-second loop. One learner in the
existing data already logged 83 answers over 68 distinct questions in one day.

WHY THE DAY IS A ROME DAY

Because the alternative is a bug with no visible cause. Under UTC the day rolls over at
02:00 Rome in summer, so a learner doing six questions at 23:50 and six at 00:10 splits one
sitting across two days and fails both — and nothing on screen would explain why.

WHY THERE IS A MINIMUM GAP

Ten questions at 23:58 and the next ten at 00:01 would otherwise bank two days for four
minutes of work, forever, and each fourteenth one pays out real Premium. The gap is measured
between the INSTANTS two days qualified, not between calendar days, and it is re-checked on
every later answer that day — so a learner who happens to finish early can still earn the
day by answering one more question once the gap has passed, rather than losing it outright.

WHY A FREEZE EXISTS

The failure mode of a streak is not that it ends. It is that ending it makes people stop
entirely. Someone thirty days in who misses a Tuesday does not restart at one — they decide
the thing they were proud of is gone and quietly stop opening the app. A freeze covers ONE
missed day.

Deliberately not generous: three at most, earned one per FREEZE_EVERY days, spent
automatically without asking. A freeze the learner has to remember to use fails at exactly
the moment it was for — they have already missed the day. The cap went from two to three
when the goal went from one answer to ten; it stays safe because a freeze can only ever
bridge YESTERDAY, so holding three buys three separate single misses and never a three-day
absence.

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
at the right hour in the right timezone. The milestone payout rides the same read for the
same reason, and is idempotent on the day it was earned rather than on the milestone number
— see `pay_milestones`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, StreakDay, User
from api.services import events
from shared.constants import EV_ANSWER_GIVEN, EV_STREAK_FROZEN, EV_STREAK_MILESTONE

log = logging.getLogger(__name__)

# The civil day this product runs on. Every learner it has is sitting an Italian exam on
# Italian time, so there is one answer here and it is not the server's.
ROME = ZoneInfo("Europe/Rome")

# Distinct questions that make a day count. Ten, forever, right or wrong. At the measured
# median pace of 7.4 seconds between answers that is 74 seconds; about 2.6 minutes for a
# slower learner, 6.3 for someone reading translations and explanations. Doable on a bus on
# a bad day, which is the entire design brief for a daily goal.
GOAL = 10

# A day cannot qualify sooner than this after the previous one did. Eight hours: long enough
# that no single sitting can span it, short enough that a late night and an early morning are
# still two days.
MIN_GAP = timedelta(hours=8)

# At most this many freezes at once. See the header: three, because a day now costs ten
# questions, and a freeze can only ever bridge yesterday.
MAX_FREEZES = 3

# One freeze per this many days of unbroken streak. Slow on purpose: a freeze should feel
# earned, and someone who has just started has nothing to protect yet. It is also a rule a
# person can say out loud — "one for every week you keep going."
FREEZE_EVERY = 7

# Every this many days, the streak pays. Recurring, for everybody, by the owner's decision
# against my recommendation of once-per-account: a once-ever reward cannot be enforced here,
# because deleting the account erases the history the guarantee is derived from. Given that,
# a small recurring bonus is the honest version of the same promise.
MILESTONE_EVERY = 14
MILESTONE_DAYS = 3

# How far back to look. 400 days of one row per qualifying day, and a streak longer than that
# is not a case worth a slower query.
LOOKBACK_DAYS = 400


def _as_date(value) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        return value.date()
    return value


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes for some paths. Everything stored is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def rome_day(moment: datetime) -> date:
    """The calendar day this instant falls on, in Rome."""
    return _aware(moment).astimezone(ROME).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """The UTC half-open interval [start, end) covering a Rome calendar day.

    Built by localising midnight rather than by adding 24 hours, so the two days a year that
    are 23 and 25 hours long are the right length. Adding a fixed day would put an hour of
    late March into the wrong bucket and duplicate an hour of late October into two.
    """
    start = datetime.combine(day, time.min, tzinfo=ROME)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=ROME)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _credited_answers(chat_id: int, start: datetime, end: datetime):
    """Distinct question ids answered at a credited pace within a UTC window.

    `credited` missing means the event predates pacing, and those are counted. The
    alternative — treating an absent key as "not credited" — would silently erase the
    history of every learner who was here before the rule existed.
    """
    return (
        select(func.distinct(Event.payload["question_id"].as_integer()))
        .where(
            Event.chat_id == chat_id,
            Event.type == EV_ANSWER_GIVEN,
            Event.created_at >= start,
            Event.created_at < end,
            func.coalesce(Event.payload["credited"].as_boolean(), True).is_(True),
        )
    )


async def counted_today(
    session: AsyncSession, chat_id: int, now: datetime | None = None
) -> int:
    """Distinct questions answered today that count toward the goal.

    Bounded to one Rome day, and the answer endpoint caps a day at 500, so this is a small
    query no matter how long the learner has been here.
    """
    now = now or datetime.now(timezone.utc)
    start, end = day_bounds(rome_day(now))
    rows = await session.scalars(_credited_answers(chat_id, start, end))
    return len(rows.all())


async def last_qualified_at(session: AsyncSession, chat_id: int) -> datetime | None:
    row = await session.scalar(
        select(func.max(StreakDay.qualified_at)).where(StreakDay.chat_id == chat_id)
    )
    return _aware(row) if row is not None else None


async def note_answer(
    session: AsyncSession, chat_id: int, now: datetime | None = None
) -> bool:
    """Record today as a qualifying day if this answer has just made it one.

    Called on the answer path, which is why it does as little as possible: one existence
    check against a primary key, and only then — for the handful of answers between the
    goal being plausible and the day being banked — one count over today's events.

    Returns True only on the answer that actually earned the day, so the caller can say so.
    """
    now = now or datetime.now(timezone.utc)
    day = rome_day(now)
    already = await session.get(StreakDay, (chat_id, day.isoformat()))
    if already is not None:
        return False

    counted = await counted_today(session, chat_id, now)
    if counted < GOAL:
        return False

    previous = await last_qualified_at(session, chat_id)
    if previous is not None and now - previous < MIN_GAP:
        # Not lost — just not yet. Every later answer today tries again, so the day is earned
        # the moment the gap has passed rather than being forfeited for finishing early.
        return False

    # ON CONFLICT DO NOTHING rather than the check above, doubled: two answers submitted
    # together would both pass the check and the second would raise. The primary key is the
    # real guarantee; the `get` above is only there to keep the common case cheap.
    result = await session.execute(
        sqlite_insert(StreakDay)
        .values(chat_id=chat_id, day=day.isoformat(), qualified_at=now, questions=counted)
        .on_conflict_do_nothing()
    )
    if not result.rowcount:
        return False
    log.info("streak day earned by %s on %s with %d questions", chat_id, day, counted)
    return True


async def qualifying_days(session: AsyncSession, chat_id: int) -> list[date]:
    """Every day this learner met the goal, newest first."""
    rows = await session.scalars(
        select(StreakDay.day)
        .where(StreakDay.chat_id == chat_id)
        .order_by(StreakDay.day.desc())
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


def run_from(days: list[date], anchor: date, frozen: set[date]) -> list[date]:
    """The qualifying days of the unbroken run ending at `anchor`, oldest first.

    Returns the days themselves rather than a count because the milestone payout needs to
    know WHICH day the fourteenth was — see `pay_milestones`. The count is its length.
    """
    if not days:
        return []
    active = set(days)
    out: list[date] = []
    cursor = anchor
    while cursor >= anchor - timedelta(days=LOOKBACK_DAYS):
        if cursor in active:
            out.append(cursor)
        elif cursor in frozen:
            pass          # covered, and does NOT add to the count — it was a day off
        else:
            break
        cursor -= timedelta(days=1)
    out.reverse()
    return out


def current_run(days: list[date], today: date, frozen: set[date]) -> list[date]:
    """The run that is still alive today, oldest first. Empty if the streak is broken.

    Ending today OR YESTERDAY on purpose, which predates freezes: a streak that breaks at
    midnight is a punishment for the calendar's convenience. Someone who did their ten last
    night and opens the app this morning before studying still has theirs.
    """
    if not days:
        return []
    yesterday = today - timedelta(days=1)
    if days[0] < yesterday and days[0] not in frozen:
        # The most recent qualifying day is too old, and nothing bridges the gap to it.
        if not any(d in frozen for d in (today, yesterday)):
            return []
    return run_from(days, max(days[0], yesterday), frozen)


def count_streak(days: list[date], today: date, frozen: set[date]) -> int:
    """Consecutive qualifying days ending today or yesterday, frozen days treated as covered.

    Pure, so the rule is testable without a database — and the rule is fiddly enough to be
    worth testing directly.
    """
    return len(current_run(days, today, frozen))


def earned(streak: int) -> int:
    """How many freezes a streak of this length has earned, in total, ever."""
    return streak // FREEZE_EVERY


def run_ending_at(days: list[date], anchor: date, frozen: set[date]) -> int:
    """Length of the consecutive run of qualifying days ending at `anchor`.

    Separate from `count_streak` to break a circularity that made the feature useless.

    The balance is DERIVED — earned minus spent — rather than incremented, so it cannot
    drift and cannot be farmed by breaking and restarting. But deriving it from the CURRENT
    streak means that the moment a day is missed the streak is 0, 0 has earned nothing, and
    the learner cannot afford the freeze whose entire purpose is to rescue that exact
    moment. The freeze would only ever be affordable while it was not needed.

    So eligibility is judged on the run they had built up to their last qualifying day. "You
    kept a fourteen-day streak, so you earned two freezes" is true whether or not today has
    already broken it.
    """
    return len(run_from(days, anchor, frozen))


async def paid_milestones(session: AsyncSession, chat_id: int) -> set[date]:
    """The days a milestone has already been paid for."""
    rows = await session.scalars(
        select(Event.payload).where(
            Event.chat_id == chat_id, Event.type == EV_STREAK_MILESTONE
        )
    )
    out: set[date] = set()
    for payload in rows:
        day = (payload or {}).get("day")
        if day:
            out.add(_as_date(day))
    return out


async def pay_milestones(
    session: AsyncSession, user: User, run: list[date], now: datetime
) -> int:
    """Grant Premium for every MILESTONE_EVERY-th day of this run not yet paid for.

    KEYED ON THE DAY, NOT ON THE MILESTONE NUMBER

    "Milestone 1" is not unique to a person: a learner who reaches fourteen days, lapses, and
    builds fourteen more has reached the first milestone twice, and the owner's decision is
    that the second one pays too. The DAY it was reached is unique, which makes the payout
    both repeatable across streaks and idempotent within one — reading the profile twice on
    the same day cannot pay twice.

    Paying for every unpaid milestone in the run, rather than only for a milestone reached
    exactly today, is what makes the reward survive not opening the app. Otherwise a learner
    whose fourteenth day was a Sunday they did not check in on would silently never be paid.
    """
    if len(run) < MILESTONE_EVERY:
        return 0
    marks = [run[i - 1] for i in range(MILESTONE_EVERY, len(run) + 1, MILESTONE_EVERY)]
    already = await paid_milestones(session, user.chat_id)
    due = [d for d in marks if d not in already]
    if not due:
        return 0

    for day in due:
        # Extends from the later of now and the current expiry, so granting to somebody who
        # already has Premium adds days rather than cutting their subscription short. That
        # this pays existing subscribers at all is the owner's decision, taken with the cost
        # in front of them; my recommendation was to pay a badge instead.
        base = user.pass_expires_at if (
            user.pass_expires_at and _aware(user.pass_expires_at) > now
        ) else now
        user.pass_expires_at = _aware(base) + timedelta(days=MILESTONE_DAYS)
        await events.record(
            session, EV_STREAK_MILESTONE, chat_id=user.chat_id,
            day=day.isoformat(),
            nth=marks.index(day) + 1,
            days=MILESTONE_DAYS,
            expires_at=user.pass_expires_at.isoformat(),
        )
        log.info("streak milestone for %s on %s: +%d days", user.chat_id, day,
                 MILESTONE_DAYS)
    return len(due) * MILESTONE_DAYS


async def refresh(
    session: AsyncSession, user: User, today: date | None = None,
    now: datetime | None = None,
) -> tuple[int, int, int]:
    """Bring the streak up to date, spending a freeze if that is what saves it.

    Returns (streak, freezes held, Premium days just granted). Commits nothing — the caller
    owns the transaction.
    """
    now = now or datetime.now(timezone.utc)
    today = today or rome_day(now)
    days = await qualifying_days(session, user.chat_id)
    frozen = await frozen_days(session, user.chat_id)

    if not days:
        user.streak_freezes = 0
        return 0, 0, 0

    def balance(frozen_days: set[date]) -> int:
        """Earned minus spent, capped.

        Judged on the run ending at their LAST QUALIFYING DAY, not on the current streak —
        see `run_ending_at`. Derived rather than incremented so it cannot drift out of step
        with the event log, and so a learner cannot farm freezes by breaking and restarting.
        """
        prior = run_ending_at(days, days[0], frozen_days)
        return min(MAX_FREEZES, max(0, earned(prior) - len(frozen_days)))

    # A freeze is spent for exactly one missing day: yesterday, when the learner qualified
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

    run = current_run(days, today, frozen)
    granted = await pay_milestones(session, user, run, now)
    # Recomputed AFTER the spend, from the event log — so the freeze just recorded is
    # counted as spent and the balance actually goes down. The first version recomputed the
    # allowance and handed the same freeze straight back, which a test caught.
    user.streak_freezes = balance(frozen)
    return len(run), user.streak_freezes, granted
