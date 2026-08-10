"""The message that says a streak is about to be lost — and everyone it refuses to send to.

This is the second unsolicited message the product sends, and the cost of getting it wrong
is specific: somebody who mutes the bot over a streak nudge also stops receiving "your
Premium ends Friday", which is the message the business runs on. So almost every test here
is a REFUSAL, and each one is a separate way this could turn into spam.

The hourly cron is the reason for two of them. A job that fires every hour inside a
three-hour evening window sends three messages unless the day is an idempotency key, and a
job that fires at exactly one instant misses the day entirely whenever that run is skipped.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from api.models import Event, User
from api.services import streak, streak_nudge
from api.services.streak import ROME
from shared.constants import EV_STREAK_NUDGE
from tests.conftest import studied_on

CHAT = 42


def rome(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ROME).astimezone(timezone.utc)


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have gone to Telegram."""
    calls: list[dict] = []

    async def fake(chat_id, lang, days, left):
        calls.append({"chat_id": chat_id, "lang": lang, "days": days, "left": left})
        return True

    monkeypatch.setattr(streak_nudge.notify, "streak_at_risk", fake)
    return calls


async def build(api_db, days: int, ending: date, chat_id: int = CHAT):
    await studied_on(api_db, chat_id,
                     [ending - timedelta(days=n) for n in range(days)])


async def run_at(api_db, when: datetime) -> dict:
    async with api_db() as s:
        return await streak_nudge.run(s, when)


# --- the message it does send ------------------------------------------------

async def test_an_evening_with_the_day_unbanked_is_nudged(api_db, registered, sent):
    """The case the feature exists for: a real streak, and the learner has not realised the
    day is not banked yet."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    out = await run_at(api_db, rome(today, 19, 30))
    assert out["due"] == 1
    assert sent and sent[0]["chat_id"] == CHAT
    assert sent[0]["days"] == 5


async def test_it_says_how_many_are_left(api_db, registered, sent):
    """A nudge that does not say how close you are cannot be acted on without opening the
    app to find out — and not realising the day is unbanked is the whole problem."""
    today = date(2026, 7, 31)
    await build(api_db, 4, today - timedelta(days=1))
    await studied_on(api_db, CHAT, [today], questions=streak.GOAL - 4)
    await run_at(api_db, rome(today, 19, 30))
    assert sent[0]["left"] == 4


# --- everyone it refuses -----------------------------------------------------

async def test_a_streak_too_short_to_care_about_is_not_nudged(api_db, registered, sent):
    """Nobody is nagged about a habit they have not built. At one or two days the message
    is noise about something the learner has not invested in."""
    today = date(2026, 7, 31)
    await build(api_db, streak_nudge.NEEDS_STREAK - 1, today - timedelta(days=1))
    assert (await run_at(api_db, rome(today, 19, 30)))["due"] == 0
    assert sent == []


async def test_somebody_who_already_did_their_ten_is_left_alone(api_db, registered, sent):
    """The message is about the gap between having studied and the day counting. There is
    no gap here, and telling someone their streak is at risk when it is not is the fastest
    way to teach them the message means nothing."""
    today = date(2026, 7, 31)
    await build(api_db, 6, today)          # includes today
    assert (await run_at(api_db, rome(today, 19, 30)))["due"] == 0
    assert sent == []


async def test_a_streak_already_broken_is_not_nudged(api_db, registered, sent):
    """Nothing left to save. This is `reminders.py`'s job, and a second "come back" from a
    different feature is how one product starts nagging with two voices."""
    today = date(2026, 7, 31)
    await build(api_db, 6, today - timedelta(days=5))
    assert (await run_at(api_db, rome(today, 19, 30)))["due"] == 0
    assert sent == []


async def test_nobody_is_woken_up_in_the_morning(api_db, registered, sent):
    """A morning nudge is a nag: there is a whole day left and nothing to act on yet."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    assert (await run_at(api_db, rome(today, 9)))["due"] == 0
    assert sent == []


async def test_nobody_is_taunted_at_midnight(api_db, registered, sent):
    """23:30 is not a reminder, it is a message about something that can no longer be done."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    assert (await run_at(api_db, rome(today, 23, 30)))["due"] == 0
    assert sent == []


async def test_the_window_is_rome_time_not_the_servers(api_db, registered, sent):
    """17:30 UTC is 19:30 in Rome in summer. A window in the server's timezone drifts by an
    hour twice a year, which means the message arrives at 18:00 for half the year without
    anyone changing anything."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    utc_evening = datetime(2026, 7, 31, 17, 30, tzinfo=timezone.utc)
    assert utc_evening.astimezone(ROME).hour == 19
    assert (await run_at(api_db, utc_evening))["due"] == 1


async def test_an_explicit_stop_is_honoured(api_db, registered, sent):
    """The same switch as every other message. Without it the only way to stop this is to
    block the bot, which also stops the renewal warning."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    async with api_db() as s:
        (await s.get(User, CHAT)).reminders_off = True
        await s.commit()
    assert (await run_at(api_db, rome(today, 19, 30)))["due"] == 0
    assert sent == []


# --- the hourly cron ---------------------------------------------------------

async def test_three_runs_in_the_window_send_one_message(api_db, registered, sent):
    """THE reason the day is on the event. The cron is hourly and the window is three hours
    wide, so without an idempotency key this feature sends three messages every evening."""
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    for hour in (19, 20, 21):
        await run_at(api_db, rome(today, hour, 5))
    assert len(sent) == 1, f"the hourly cron sent {len(sent)} messages in one evening"


async def test_tomorrow_is_a_new_day(api_db, registered, sent):
    """Idempotent per day, not for ever. Someone at risk two evenings running is at risk
    twice."""
    first = date(2026, 7, 31)
    await build(api_db, 5, first - timedelta(days=1))
    await run_at(api_db, rome(first, 19, 30))
    await studied_on(api_db, CHAT, [first])          # they acted on it
    await run_at(api_db, rome(first + timedelta(days=1), 19, 30))
    assert len(sent) == 2


async def test_the_attempt_is_recorded_even_when_the_bot_is_blocked(api_db, registered,
                                                                    monkeypatch):
    """A blocked bot fails every time. Recording only successes would put that person at the
    front of the queue for ever — the record is of the ATTEMPT, as in reminders.py."""
    async def fails(*_a, **_kw):
        return False

    monkeypatch.setattr(streak_nudge.notify, "streak_at_risk", fails)
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    out = await run_at(api_db, rome(today, 19, 30))
    assert out == {"due": 1, "delivered": 0, "window": True}

    from sqlalchemy import select
    async with api_db() as s:
        rows = (await s.scalars(select(Event).where(
            Event.chat_id == CHAT, Event.type == EV_STREAK_NUDGE))).all()
    assert len(rows) == 1
    assert rows[0].payload["delivered"] is False

    # And the failure does not buy a second attempt in the same evening.
    await run_at(api_db, rome(today, 21))
    async with api_db() as s:
        again = (await s.scalars(select(Event).where(
            Event.chat_id == CHAT, Event.type == EV_STREAK_NUDGE))).all()
    assert len(again) == 1


async def test_a_run_outside_the_window_costs_nothing(api_db, registered, sent):
    """The cron fires 24 times a day. Twenty-one of those must do no work at all rather than
    walking every user."""
    out = await run_at(api_db, rome(date(2026, 7, 31), 4))
    assert out == {"due": 0, "delivered": 0, "window": False}


async def test_each_learner_is_told_their_own_number(api_db, registered, sent):
    """Two people at risk on the same evening, with different streaks. Anything shared
    between iterations — a streak computed once, a "left" read for the wrong chat — shows up
    here as one person being told the other's numbers."""
    today = date(2026, 7, 31)
    yesterday = today - timedelta(days=1)
    async with api_db() as s:
        s.add(User(chat_id=999, lang="ru"))
        await s.commit()
    await build(api_db, 5, yesterday, chat_id=CHAT)
    await build(api_db, 9, yesterday, chat_id=999)
    await studied_on(api_db, 999, [today], questions=3)      # partway through today

    await run_at(api_db, rome(today, 19, 30))
    by_chat = {c["chat_id"]: c for c in sent}
    assert set(by_chat) == {CHAT, 999}
    assert by_chat[CHAT]["days"] == 5 and by_chat[CHAT]["left"] == streak.GOAL
    assert by_chat[999]["days"] == 9 and by_chat[999]["left"] == streak.GOAL - 3


async def test_somebody_elses_evening_is_not_your_idempotency_key(api_db, registered, sent):
    """One person nudged must not silence the next.

    The two people are nudged in SEPARATE runs on purpose. Within a single run every
    candidate is filtered before anything is written, so an idempotency check that forgot to
    scope by chat would still let both through and look correct. It is only on the next run —
    with one person's event already in the log — that an unscoped check silences everybody
    else for the rest of the day.
    """
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1), chat_id=CHAT)
    await run_at(api_db, rome(today, 19, 30))
    assert [c["chat_id"] for c in sent] == [CHAT]

    async with api_db() as s:
        s.add(User(chat_id=999, lang="ru"))
        await s.commit()
    await build(api_db, 5, today - timedelta(days=1), chat_id=999)
    await run_at(api_db, rome(today, 20, 30))
    assert sorted(c["chat_id"] for c in sent) == [CHAT, 999], \
        "one learner's nudge silenced another's"


async def test_the_window_is_refused_at_the_gate_too(api_db, registered):
    """`due` refuses outside the window on its own, not only because `run` checks first.

    A guard that is only reachable through another guard is one nobody can rely on, and this
    one is the difference between a 4am cron run costing one comparison and costing a walk
    over every user in the database.
    """
    today = date(2026, 7, 31)
    await build(api_db, 5, today - timedelta(days=1))
    async with api_db() as s:
        assert await streak_nudge.due(s, rome(today, 4)) == []
        assert len(await streak_nudge.due(s, rome(today, 19, 30))) == 1


# --- the text ----------------------------------------------------------------

@pytest.mark.parametrize("lang", ["ru", "en", "it", "uz"])
def test_every_language_has_the_message_with_both_numbers(lang):
    """A placeholder that survives into a sent message reads as a broken bot, and this text
    is one `.format` away from doing exactly that in a language nobody on the team reads."""
    import json
    from pathlib import Path

    text = json.loads(Path(f"bot/locales/{lang}.json").read_text())["streak_at_risk"]
    assert "{days}" in text and "{left}" in text
    assert text.format(days=5, left=4).count("{") == 0
    assert "\\n" not in text, "an escaped newline reaches the learner as the characters \\n"
