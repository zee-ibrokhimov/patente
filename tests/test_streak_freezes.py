"""A streak survives one bad evening.

The failure mode of a streak is not that it ends — it is that ending it makes people stop
entirely. Someone thirty days in who misses a Tuesday does not restart at day one; they
conclude the thing they were proud of is gone and quietly stop opening the app. A freeze
covers ONE missed day so a late shift does not undo a month.

Deliberately not generous: two at most, one earned per FREEZE_EVERY days, spent
automatically. A freeze the learner has to remember to use fails at exactly the moment it
was for, and an unlimited supply is not a streak.

WHICH DAYS WERE COVERED LIVES IN THE EVENT LOG, not in a "frozen until" column. That is what
makes it idempotent — computing the streak twice cannot spend two freezes, because the
second pass finds the day already covered — and it is the same reasoning the lapse notices
use for the same reason.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, User
from api.services import streak
from api.services.streak import FREEZE_EVERY, MAX_FREEZES, count_streak, earned
from shared.constants import EV_ANSWER_GIVEN, EV_STREAK_FROZEN

TODAY = date(2026, 7, 31)
CHAT = 42


def days_back(*offsets: int) -> list[date]:
    """Active days, newest first, as the query returns them."""
    return [TODAY - timedelta(days=n) for n in sorted(offsets)]


# --- the rule, as a pure function -------------------------------------------

def test_a_run_ending_today_counts():
    assert count_streak(days_back(0, 1, 2), TODAY, set()) == 3


def test_a_run_ending_yesterday_still_counts():
    """Predates freezes and stays: a streak that breaks at midnight in a timezone the
    server picked is a punishment for the server's convenience."""
    assert count_streak(days_back(1, 2, 3), TODAY, set()) == 3


def test_a_gap_ends_it():
    assert count_streak(days_back(0, 1, 3, 4), TODAY, set()) == 2


def test_nothing_at_all_is_zero():
    assert count_streak([], TODAY, set()) == 0


def test_an_old_run_is_not_a_streak():
    assert count_streak(days_back(5, 6, 7), TODAY, set()) == 0


# --- what the freeze changes -------------------------------------------------

def test_a_frozen_day_bridges_the_gap():
    """THE point. Active today and the day before yesterday, with yesterday frozen: one
    run of two active days, not two runs of one."""
    frozen = {TODAY - timedelta(days=1)}
    assert count_streak(days_back(0, 2, 3), TODAY, frozen) == 3


def test_a_frozen_day_does_not_itself_count():
    """It protects the streak; it does not inflate it. A day off is a day off, and letting
    a freeze add to the number would make the streak a count of days PAID for."""
    frozen = {TODAY - timedelta(days=1)}
    with_freeze = count_streak(days_back(0, 2, 3), TODAY, frozen)
    without_gap = count_streak(days_back(0, 1, 2, 3), TODAY, set())
    assert with_freeze == 3
    assert without_gap == 4


def test_a_two_day_gap_is_not_bridged_by_one_freeze():
    frozen = {TODAY - timedelta(days=1)}
    assert count_streak(days_back(0, 3, 4), TODAY, frozen) == 1


# --- earning ----------------------------------------------------------------

def test_freezes_are_earned_slowly():
    assert earned(0) == 0
    assert earned(FREEZE_EVERY - 1) == 0
    assert earned(FREEZE_EVERY) == 1
    assert earned(FREEZE_EVERY * 3) == 3


async def _answered_on(api_db, offsets, chat_id=CHAT):
    async with api_db() as s:
        for n in offsets:
            when = datetime.combine(TODAY - timedelta(days=n),
                                    datetime.min.time(), tzinfo=timezone.utc)
            s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                        payload={"correct": True}, created_at=when + timedelta(hours=12)))
        await s.commit()


async def test_a_new_learner_has_no_freezes(api_db, registered):
    await _answered_on(api_db, [0, 1])
    async with api_db() as s:
        days, freezes = await streak.refresh(s, await s.get(User, CHAT), TODAY)
    assert days == 2
    assert freezes == 0


async def test_a_week_earns_one(api_db, registered):
    await _answered_on(api_db, list(range(FREEZE_EVERY)))
    async with api_db() as s:
        days, freezes = await streak.refresh(s, await s.get(User, CHAT), TODAY)
    assert days == FREEZE_EVERY
    assert freezes == 1


async def test_the_balance_is_capped(api_db, registered):
    await _answered_on(api_db, list(range(FREEZE_EVERY * (MAX_FREEZES + 4))))
    async with api_db() as s:
        _days, freezes = await streak.refresh(s, await s.get(User, CHAT), TODAY)
    assert freezes == MAX_FREEZES


# --- spending ---------------------------------------------------------------

async def test_a_missed_day_spends_a_freeze_and_saves_the_streak(api_db, registered):
    """The whole feature, end to end: active for a fortnight, missed yesterday, opens the
    app today — and the streak is intact rather than back at zero.

    The balance is DERIVED (earned minus spent) rather than stored, so it is set here by
    building a real fourteen-day run, not by assigning the column. Fourteen days earns two;
    one is spent covering yesterday, leaving one.
    """
    await _answered_on(api_db, list(range(2, 16)))     # nothing today, nothing yesterday
    async with api_db() as s:
        user = await s.get(User, CHAT)
        days, freezes = await streak.refresh(s, user, TODAY)
        await s.commit()
    assert days >= 14, f"the streak collapsed to {days} despite a freeze being available"
    assert freezes == 1, "the spent freeze was handed straight back"


async def test_spending_is_recorded_against_the_day(api_db, registered):
    await _answered_on(api_db, list(range(2, 16)))
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.streak_freezes = 1
        await streak.refresh(s, user, TODAY)
        await s.commit()

    async with api_db() as s:
        rows = (await s.scalars(
            select(Event).where(Event.chat_id == CHAT, Event.type == EV_STREAK_FROZEN))).all()
    assert len(rows) == 1
    assert rows[0].payload["day"] == (TODAY - timedelta(days=1)).isoformat()


async def test_reading_twice_does_not_spend_twice(api_db, registered):
    """THE reason the days live in the event log. The profile is read on every visit, and a
    balance decremented on read would drain by simply opening the app."""
    await _answered_on(api_db, list(range(2, 16)))
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.streak_freezes = 2
        for _ in range(4):
            await streak.refresh(s, user, TODAY)
        await s.commit()

    async with api_db() as s:
        user = await s.get(User, CHAT)
        rows = (await s.scalars(
            select(Event).where(Event.chat_id == CHAT, Event.type == EV_STREAK_FROZEN))).all()
    assert len(rows) == 1, f"four reads spent {len(rows)} freezes"


async def test_without_an_earned_freeze_the_streak_really_ends(api_db, registered):
    """The guard must not become an unconditional bridge. Someone who has not yet earned a
    freeze loses their streak to a missed day — otherwise the streak means nothing and the
    number stops being worth protecting."""
    await _answered_on(api_db, list(range(2, 2 + FREEZE_EVERY - 2)))   # too short to earn
    async with api_db() as s:
        user = await s.get(User, CHAT)
        days, freezes = await streak.refresh(s, user, TODAY)
    assert freezes == 0
    assert days == 0


async def test_a_long_absence_is_not_bridged(api_db, registered):
    """A freeze covers a slip, not a fortnight away. Pretending otherwise makes the number
    a lie, and a streak nobody believes is worth nothing."""
    await _answered_on(api_db, list(range(10, 30)))     # long enough to have earned two
    async with api_db() as s:
        user = await s.get(User, CHAT)
        days, freezes = await streak.refresh(s, user, TODAY)
    assert days == 0
    assert freezes == MAX_FREEZES, "a freeze was spent on a gap it could not bridge"


async def test_an_active_streak_spends_nothing(api_db, registered):
    """Nothing to rescue, so nothing is spent — and the balance stays available for the day
    it is actually needed."""
    await _answered_on(api_db, list(range(FREEZE_EVERY * 2)))     # unbroken, ends today
    async with api_db() as s:
        user = await s.get(User, CHAT)
        _days, freezes = await streak.refresh(s, user, TODAY)
        await s.commit()

    async with api_db() as s:
        spent = (await s.scalars(
            select(Event).where(Event.chat_id == CHAT,
                                Event.type == EV_STREAK_FROZEN))).all()
    assert spent == [], "a freeze was spent on an unbroken streak"
    assert freezes == MAX_FREEZES


# --- what the profile reports ------------------------------------------------

async def test_the_profile_reports_the_balance(client, registered, api_db):
    import json
    import time

    from api.services.telegram_auth import sign
    from shared.config import settings

    token = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = token
    settings.env = "prod"
    headers = {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": CHAT}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, token)}

    body = (await client.get("/webapp/profile", headers=headers)).json()
    assert "streak_freezes" in body, \
        "a freeze nobody can see protects nothing psychologically — the point is knowing"
