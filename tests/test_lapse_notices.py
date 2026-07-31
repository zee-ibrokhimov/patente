"""Telling someone their Premium ended, instead of letting it go quiet.

Nothing did. A pass reached its date and the features simply stopped — translations gone
from under the questions, the explanation button refusing, the vocabulary trainer
answering 402. No message and nothing to click.

That is the worst moment to say nothing. Someone who has been paying is suddenly looking
at a paywall with no explanation, and their first assumption is that the app broke rather
than that their subscription ended. It is also the single moment they are most likely to
renew, and the app was silent through it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Purchase, User
from api.services import lapse, notify
from shared.constants import EV_PASS_ENDING, EV_PASS_LAPSED, UI_LANGUAGES

NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    """Never actually message Telegram from a test."""
    sent = []

    async def fake(chat_id, lang, kind, expires_at, tier, days=0):
        sent.append({"chat_id": chat_id, "kind": kind, "lang": lang, "days": days})
        return True

    monkeypatch.setattr(notify, "payment", fake)
    monkeypatch.setattr(lapse.notify, "payment", fake)
    return sent


@pytest.fixture
def sent(_capture):
    return _capture


async def make(api_db, chat_id: int, expires, *, paid: bool = False, lang: str = "ru"):
    async with api_db() as s:
        s.add(User(chat_id=chat_id, lang=lang, pass_expires_at=expires))
        if paid:
            s.add(Purchase(chat_id=chat_id, tribute_purchase_id=f"p{chat_id}",
                           tier="pass_1m", amount_cents=299, currency="eur",
                           extended_to=expires or NOW))
        await s.commit()


# --- it ends ----------------------------------------------------------------

async def test_a_lapsed_user_is_told(api_db, sent):
    await make(api_db, 601, NOW - timedelta(hours=2))
    async with api_db() as s:
        await lapse.run(s)
    assert [m["kind"] for m in sent] == ["lapsed"]


async def test_the_message_says_what_is_still_free(sent):
    """The moment Premium stops is when someone decides whether the app is worth
    reopening. "It's over" alone loses them; the free tier is genuinely substantial."""
    for lang in ("ru", "en", "it", "uz"):
        text = notify.compose("lapsed", lang, NOW, "")
        assert "7106" in text
        assert "/plan" in text


async def test_nobody_is_told_twice(api_db, sent):
    """The job runs hourly. A flag would have to be cleared correctly on every renewal;
    the event carries the expiry it was about, so repeats are impossible without one."""
    await make(api_db, 602, NOW - timedelta(hours=1))
    async with api_db() as s:
        await lapse.run(s)
        await lapse.run(s)
        await lapse.run(s)
    assert len(sent) == 1


async def test_a_pass_that_lapsed_renewed_and_lapsed_again_is_told_twice(api_db, sent):
    """Keyed on the DATE, not just the user. A flag would either repeat itself or go
    silent forever after the first time."""
    first = NOW - timedelta(days=2)
    await make(api_db, 603, first)
    async with api_db() as s:
        await lapse.run(s)

    async with api_db() as s:
        user = await s.get(User, 603)
        user.pass_expires_at = NOW - timedelta(minutes=5)   # renewed, then lapsed again
        await s.commit()
    async with api_db() as s:
        await lapse.run(s)
    assert len(sent) == 2


async def test_an_active_pass_is_left_alone(api_db, sent):
    await make(api_db, 604, NOW + timedelta(days=40))
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


async def test_a_free_user_is_not_told_anything(api_db, sent):
    """No pass, no expiry, nothing to announce."""
    await make(api_db, 605, None)
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


async def test_an_ancient_expiry_is_not_dug_up(api_db, sent):
    """Messaging someone about a subscription that ended a month ago is worse than
    silence — and on first run it would message every lapsed user at once."""
    await make(api_db, 606, NOW - timedelta(days=90))
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


# --- it is about to end -----------------------------------------------------

async def test_a_pass_ending_soon_gets_a_warning(api_db, sent):
    await make(api_db, 610, NOW + timedelta(days=2))
    async with api_db() as s:
        await lapse.run(s)
    assert [m["kind"] for m in sent] == ["ending"]
    assert sent[0]["days"] >= 1


async def test_a_subscriber_is_not_warned_about_a_renewal(api_db, sent):
    """Tribute will renew them and tells them so. Warning a paying subscriber that their
    subscription is about to continue is noise, and noise is what makes people mute a
    bot."""
    await make(api_db, 611, NOW + timedelta(days=2), paid=True)
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


async def test_the_warning_says_what_switches_off(api_db):
    for lang in ("ru", "en", "it", "uz"):
        text = notify.compose("ending", lang, NOW + timedelta(days=3), "", days=3)
        assert "3" in text
        assert "/plan" in text


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_both_messages_exist_in_every_language(lang):
    for kind in ("ending", "lapsed"):
        assert lang in notify.MESSAGES[kind], f"{kind} has no {lang}"


@pytest.mark.parametrize("kind", ["ending", "lapsed"])
@pytest.mark.parametrize("lang", ["ru", "en", "it", "uz"])
def test_no_placeholder_survives(kind, lang):
    text = notify.compose(kind, lang, NOW, "", days=3)
    assert "{" not in text and "}" not in text
    assert "None" not in text


# --- the record -------------------------------------------------------------

async def test_an_event_is_written_even_if_telegram_refuses(api_db, monkeypatch):
    """Someone who blocked the bot must not be retried hourly forever."""
    async def blocked(*a, **kw):
        return False

    monkeypatch.setattr(lapse.notify, "payment", blocked)
    await make(api_db, 620, NOW - timedelta(hours=1))
    async with api_db() as s:
        await lapse.run(s)
    async with api_db() as s:
        rows = (await s.scalars(
            select(Event).where(Event.chat_id == 620, Event.type == EV_PASS_LAPSED))).all()
    assert len(rows) == 1
