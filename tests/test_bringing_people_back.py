"""Nudging a learner who has stopped opening the app — and the limits that make it defensible.

"notifications for my users when they are not using my app". Nothing reached them:
`lapse.py` messages on a DATE in `users.pass_expires_at`, never on silence, so somebody who
drifted away with two months left on their pass heard nothing — and drifting away is what
almost everyone does.

Nearly all of this file is about the LIMITS rather than the feature. A nudge is welcome
once; the same nudge every week is why people mute bots, and a muted bot cannot deliver the
renewal warning either. So an over-eager reminder does not merely annoy — it costs the
messages that actually matter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.models import Event, User
from api.services import notify, reminders
from shared.constants import EV_ANSWER_GIVEN, EV_REMINDER_SENT


def ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def a_learner(api_db, chat_id: int, *, quiet_days: float, answers: int = 20,
                    reminders_off: bool = False, reminded: list[float] | None = None):
    async with api_db() as s:
        s.add(User(chat_id=chat_id, lang="ru", reminders_off=reminders_off))
        for i in range(answers):
            s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                        created_at=ago(quiet_days + 1 + i * 0.01)))
        for when in (reminded or []):
            s.add(Event(chat_id=chat_id, type=EV_REMINDER_SENT, created_at=ago(when),
                        payload={"delivered": True}))
        await s.commit()


async def who_is_due(api_db) -> set[int]:
    async with api_db() as s:
        return {u.chat_id for u in await reminders.due(s)}


async def run_and_capture(api_db) -> list[int]:
    sent: list[int] = []

    async def fake(chat_id, lang):
        sent.append(chat_id)
        return True

    original = notify.reminder
    notify.reminder = fake
    try:
        async with api_db() as s:
            await reminders.run(s)
    finally:
        notify.reminder = original
    return sent


# --- who gets one ------------------------------------------------------------

async def test_a_quiet_learner_is_nudged(api_db):
    await a_learner(api_db, 9800, quiet_days=20)
    assert 9800 in await who_is_due(api_db)


async def test_somebody_still_using_it_is_not(api_db):
    await a_learner(api_db, 9810, quiet_days=1)
    assert 9810 not in await who_is_due(api_db)


async def test_a_bounce_is_not_a_lapse(api_db):
    """Somebody who opened the app once and never came back did not drift away — they
    never arrived. "Come back" to a person who was never here reads as a bot that noticed
    them leaving, and it also catches the accidental /start."""
    await a_learner(api_db, 9820, quiet_days=30, answers=2)
    assert 9820 not in await who_is_due(api_db)


async def test_an_opt_out_is_honoured(api_db):
    await a_learner(api_db, 9830, quiet_days=30, reminders_off=True)
    assert 9830 not in await who_is_due(api_db)


# --- the limits --------------------------------------------------------------

async def test_it_does_not_nudge_twice_in_a_row(api_db):
    """MIN_GAP. The same message a week later is the thing that gets a bot muted."""
    await a_learner(api_db, 9840, quiet_days=30, reminded=[3])
    assert 9840 not in await who_is_due(api_db)


async def test_it_nudges_again_once_the_gap_has_passed(api_db):
    await a_learner(api_db, 9850, quiet_days=40, reminded=[30])
    assert 9850 in await who_is_due(api_db)


async def test_three_ignored_reminders_is_an_answer(api_db):
    """MAX_EVER. Somebody who has ignored three is not undecided — they have finished with
    it, and the honest reading of silence is that they left."""
    await a_learner(api_db, 9860, quiet_days=200, reminded=[30, 60, 90])
    assert 9860 not in await who_is_due(api_db)


async def test_one_run_cannot_message_everybody(api_db):
    """A blast radius, not a throughput limit. A bug that made everyone look quiet would
    otherwise reach the entire user base before anybody noticed."""
    assert reminders.MAX_PER_RUN <= 50


# --- sending -----------------------------------------------------------------

async def test_running_it_sends_and_records(api_db):
    await a_learner(api_db, 9870, quiet_days=30)
    assert 9870 in await run_and_capture(api_db)
    assert 9870 not in await who_is_due(api_db), \
        "it would send again on the very next hourly run"


async def test_a_failed_send_still_counts_against_the_limits(api_db):
    """A blocked bot fails every time. Recording only successes would keep that person
    permanently at the front of the queue, retried hourly for ever."""
    await a_learner(api_db, 9880, quiet_days=30)

    async def fails(chat_id, lang):
        return False

    original = notify.reminder
    notify.reminder = fails
    try:
        async with api_db() as s:
            out = await reminders.run(s)
    finally:
        notify.reminder = original

    assert out["due"] == 1 and out["delivered"] == 0
    assert 9880 not in await who_is_due(api_db), \
        "a failed send left them due again immediately"


async def test_running_it_twice_sends_once(api_db):
    """The cron runs hourly. Idempotence is the whole reason that is safe."""
    await a_learner(api_db, 9890, quiet_days=30)
    first = await run_and_capture(api_db)
    second = await run_and_capture(api_db)
    assert 9890 in first and 9890 not in second


# --- the message itself ------------------------------------------------------

def test_the_message_offers_a_way_out():
    """Without a stop button the only way to end an unwanted message is to block the bot —
    which also ends the payment notices and the renewal warning. The cost of one unwanted
    message would be every wanted one."""
    import inspect

    source = inspect.getsource(notify.reminder)
    assert "reminder_stop" in source, "the nudge cannot be turned off from the nudge"
    assert "r:stop" in source, "the stop button has no callback the bot can act on"


def test_the_message_exists_in_every_language():
    import json
    import pathlib

    from shared.constants import UI_LANGUAGES

    root = pathlib.Path(__file__).resolve().parent.parent / "bot" / "locales"
    for lang in UI_LANGUAGES:
        data = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("reminder", "reminder_stop", "reminder_stopped"):
            assert data.get(key, "").strip(), f"{key} missing or blank in {lang}"
