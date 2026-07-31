"""Telling the buyer what happened.

The trigger for this was a real trial: the owner linked a card, the webhook applied it
correctly, and the app said nothing at all. Seven days later it would have charged them.

So the tests that matter are about CONTENT — that the trial message states the date, the
amount and the fact that a charge is coming — and about the message never being able to
break a payment.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.services import notify
from shared.constants import TIER_1M, TIER_PRICE_CENTS, UI_LANGUAGES

WHEN = datetime(2026, 8, 7, 10, 27, tzinfo=timezone.utc)
LANGS = ["ru", "en", "it", "uz"]


# --- the trial message has to say three things ------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_states_the_end_date(lang):
    assert "07.08.2026" in notify.compose("trial", lang, WHEN, TIER_1M)


@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_states_the_amount(lang):
    """"Free trial" with no number is how a charge becomes a surprise."""
    assert "€2.99" in notify.compose("trial", lang, WHEN, TIER_1M)


@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_says_a_charge_is_coming_and_can_be_stopped(lang):
    """Both halves. "You will be charged" alone is a threat; "you can cancel" alone
    omits the thing being cancelled."""
    text = notify.compose("trial", lang, WHEN, TIER_1M).lower()
    charge = {"ru": "спишется", "en": "charged", "it": "addebitat", "uz": "yechib"}[lang]
    stop = {"ru": "отменить", "en": "cancel", "it": "disdire", "uz": "bekor"}[lang]
    assert charge in text
    assert stop in text


@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_says_where_to_cancel(lang):
    """A cancel instruction with no destination is not an instruction. Cancellation
    lives in Tribute, not in this bot — we cannot cancel it for them."""
    assert "@tribute" in notify.compose("trial", lang, WHEN, TIER_1M)


# --- cancellation must reassure, not alarm ----------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_the_cancellation_message_says_access_continues(lang):
    """The whole reason to send it: silence after cancelling reads as "access gone",
    which is the opposite of what happens."""
    assert "07.08.2026" in notify.compose("cancelled", lang, WHEN, TIER_1M)


@pytest.mark.parametrize("lang", LANGS)
def test_the_paid_message_states_the_expiry(lang):
    assert "07.08.2026" in notify.compose("paid", lang, WHEN, TIER_1M)


# --- the machinery ----------------------------------------------------------

@pytest.mark.parametrize("kind", ["trial", "paid", "cancelled"])
@pytest.mark.parametrize("lang", LANGS)
def test_no_message_leaks_a_placeholder(kind, lang):
    """An unfilled {price} or the string "None" in a message about money is worse than
    no message."""
    text = notify.compose(kind, lang, WHEN, TIER_1M)
    assert "{" not in text and "}" not in text
    assert "None" not in text


@pytest.mark.parametrize("kind", ["trial", "paid", "cancelled"])
def test_an_unwritten_language_falls_back_to_a_real_sentence(kind):
    """A KeyError here would fail the webhook of someone who has just paid."""
    text = notify.compose(kind, "de", WHEN, TIER_1M)
    assert text and "{" not in text


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_every_ui_language_is_written(lang):
    """UI_LANGUAGES is the promise; these messages are part of it."""
    for kind in ("trial", "paid", "cancelled"):
        assert lang in notify.MESSAGES[kind], f"{kind} has no {lang}"


def test_a_missing_expiry_does_not_print_none():
    assert "None" not in notify.compose("paid", "en", None, TIER_1M)


def test_the_price_matches_the_constant():
    """The message, the button and the checkout page must agree. This is the number
    people compare against their card statement."""
    text = notify.compose("trial", "en", WHEN, TIER_1M)
    cents = TIER_PRICE_CENTS[TIER_1M]
    assert f"€{cents // 100}.{cents % 100:02d}" in text


# --- it must never break a payment ------------------------------------------

async def test_sending_without_a_token_returns_false_rather_than_raising(monkeypatch):
    from shared.config import settings

    monkeypatch.setattr(settings, "bot_token_prod", "")
    monkeypatch.setattr(settings, "bot_token_dev", "")
    assert await notify.send(1, "hello") is False


async def test_a_telegram_failure_is_swallowed(monkeypatch):
    """Tribute retries a non-2xx. If a failed message could fail the webhook, Telegram
    being briefly slow would redeliver a payment that was already applied."""
    from shared.config import settings

    monkeypatch.setattr(settings, "bot_token_prod", "token")
    monkeypatch.setattr(settings, "env", "prod")

    class Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise RuntimeError("telegram is down")

    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **k: Boom())
    assert await notify.send(1, "hello") is False


async def test_a_blocked_user_is_not_an_error(monkeypatch, caplog):
    """Someone who blocked the bot, or paid before ever opening it, is a 403. Normal for
    a payment webhook — it must not read as a broken integration."""
    import logging

    from shared.config import settings

    monkeypatch.setattr(settings, "bot_token_prod", "token")
    monkeypatch.setattr(settings, "env", "prod")

    class Blocked:
        status_code, text = 403, "bot was blocked by the user"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return Blocked()

    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda **k: Client())
    with caplog.at_level(logging.WARNING, logger="api.services.notify"):
        assert await notify.send(1, "hello") is False
    assert caplog.text == "", "a blocked user should not log at WARNING"
