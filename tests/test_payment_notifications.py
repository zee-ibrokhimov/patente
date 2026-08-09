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

# A date that is always in the FUTURE, because half these messages are about access that
# has not run out yet and `compose` branches on exactly that: a cancellation whose final
# date has passed becomes "ended", deliberately, since telling someone they keep access
# until a date that is behind them is worse than saying nothing.
#
# This was pinned to 2026-08-07 and the suite went green for as long as that was still
# ahead. On 2026-08-09 four tests started failing with nothing having changed — the code was
# right and the tests had expired. A fixed date in a test about "until when" is a fuse.
WHEN = datetime.now(timezone.utc) + timedelta(days=7)
# What the app renders that date as. Derived from WHEN rather than written out, so the two
# cannot drift apart — writing the string by hand is what made these tests expire.
WHEN_TEXT = WHEN.strftime("%d.%m.%Y")
LANGS = ["ru", "en", "it", "uz"]


# --- the trial message has to say three things ------------------------------

@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_states_the_end_date(lang):
    assert WHEN_TEXT in notify.compose("trial", lang, WHEN, TIER_1M)


@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_never_quotes_a_price_it_cannot_source(lang):
    """It used to say EUR 2.99, always, and that was a lie in writing.

    Tribute sends `period: "trial"` on a trial. That period has no tier of its own, so it
    falls back to the SHORTEST one — meaning a learner who chose the six-month plan was
    told they would be charged 2.99 and would then be charged 10.99. A wrong number in
    writing is what a chargeback is argued from; a phrase is honest.
    """
    text = notify.compose("trial", lang, WHEN, TIER_1M)
    assert "€" not in text, "a figure here cannot be trusted — see TRIBUTE_PERIODS_WITHOUT_TIER"


@pytest.mark.parametrize("lang", LANGS)
def test_the_trial_message_still_says_a_charge_is_coming(lang):
    """Dropping the number must not turn into dropping the warning."""
    text = notify.compose("trial", lang, WHEN, TIER_1M).lower()
    phrase = {"ru": "цена выбранного плана", "en": "the price of the plan you chose",
              "it": "il prezzo del piano scelto", "uz": "siz tanlagan reja narxi"}[lang]
    assert phrase in text


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
    assert WHEN_TEXT in notify.compose("cancelled", lang, WHEN, TIER_1M)


@pytest.mark.parametrize("lang", LANGS)
def test_the_paid_message_states_the_expiry(lang):
    assert WHEN_TEXT in notify.compose("paid", lang, WHEN, TIER_1M)


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


def test_the_paid_message_needs_no_price_at_all():
    """Someone who has just paid knows what they paid. It confirms receipt and the
    expiry, which is the thing they cannot see for themselves."""
    text = notify.compose("paid", "en", WHEN, TIER_1M)
    assert WHEN_TEXT in text
    assert "€" not in text


def test_a_known_tier_still_renders_its_real_price():
    """The trial fix must not turn into "never show a price". A tier that genuinely came
    from Tribute's `period` is trustworthy and its figure is correct."""
    cents = TIER_PRICE_CENTS[TIER_1M]
    assert notify._price_words(TIER_1M, "en") == f"€{cents // 100}.{cents % 100:02d}"


@pytest.mark.parametrize("lang", LANGS)
def test_an_unknown_tier_renders_an_honest_phrase_not_a_zero(lang):
    """The failure mode this replaces: TIER_PRICE_CENTS.get(tier, 0) rendered "€0.00"
    for anything unrecognised, which is worse than a wrong price."""
    words = notify._price_words("something_unmapped", lang)
    assert "€" not in words
    assert words.strip()


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


# --- when the cancellation arrives changes what it should say ---------------
#
# Tribute registers "cancels on 07.08" the moment the user asks, and emits
# cancelled_subscription on that DATE rather than at that moment. So the common case is
# that access has already ended by the time the message goes out.


def test_a_cancellation_that_still_has_time_left_reassures():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    text = notify.compose("cancelled", "en", now + timedelta(days=6), TIER_1M, now=now)
    assert "07.08.2026" in text
    assert "keep access" in text.lower()


def test_a_cancellation_arriving_at_the_end_does_not_promise_access():
    """"You keep access until 07.08" delivered ON 07.08 tells the reader nothing, and
    reads as though something is still owed to them."""
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    text = notify.compose("cancelled", "en", now - timedelta(minutes=1), TIER_1M, now=now)
    assert "keep access" not in text.lower()
    assert "ended" in text.lower()


@pytest.mark.parametrize("lang", LANGS)
def test_the_ended_message_says_what_is_still_free(lang):
    """The moment Premium lapses is the moment someone decides whether the app is worth
    reopening. "It's over" alone loses them; the free tier is genuinely substantial."""
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    text = notify.compose("cancelled", lang, now - timedelta(minutes=1), TIER_1M, now=now)
    assert "7106" in text
    assert "/plan" in text


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_ended_message_exists_in_every_language(lang):
    assert lang in notify.MESSAGES["ended"]


def test_a_cancellation_with_no_date_still_renders():
    assert "{" not in notify.compose("cancelled", "en", None, TIER_1M)
