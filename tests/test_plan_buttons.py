"""The Buy button, now that there is nothing to buy from.

Payment moved off Tribute on 2026-08-09 to a direct arrangement: a learner messages the
owner, they agree terms, and access is granted by hand. There is no hosted page, no card
form and no webhook.

So the button's job changed from "open a checkout" to "start the conversation", and the two
fail in very different ways. A broken checkout takes money and delivers nothing; a broken
handle simply does not open a chat. That is why the old gate — a webhook secret AND a
checkout link — is gone, and the only question left is whether anybody is configured to be
messaged.

WHAT SURVIVED FROM THE OLD DESIGN, because it was right:

  · a Subscribe button that opens nothing is worse than no button, since it reads as a
    broken product rather than an unfinished one;
  · the message and the keyboard make ONE decision, so they cannot contradict each other —
    that is `render.selling`, and it is still what decides whether to sell at all.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from bot import keyboards, render
from bot.handlers.progress import _can_subscribe
from shared.config import settings
from shared.constants import TIER_FEATURED, TIER_PRICE_CENTS, UI_LANGUAGES

ROOT = pathlib.Path(__file__).resolve().parent.parent
FREE = {"premium": False, "premium_via": "none", "has_pass": False, "purchased": False,
        "pass_expires_at": None}


@pytest.fixture
def selling(monkeypatch):
    """A deployment that can take money: somebody to message."""
    monkeypatch.setattr(settings, "sales_contact", "@iambrock")
    monkeypatch.setattr(settings, "support_contact", "@help")


# --- what opens the gate now -------------------------------------------------

def test_a_handle_is_all_it_takes(selling):
    assert _can_subscribe() is True


def test_no_handle_means_no_selling(monkeypatch):
    """The only way this can be false now. A button with nowhere to go is the one thing
    the old design got right and this keeps."""
    monkeypatch.setattr(settings, "sales_contact", "")
    monkeypatch.setattr(settings, "support_contact", "")
    assert _can_subscribe() is False
    assert keyboards.plan_actions("en", can_subscribe=False) is None


def test_a_stale_tribute_link_cannot_start_selling_again(monkeypatch):
    """`can_sell` is hard-wired False. An old .env left over from Tribute must not quietly
    reopen a checkout that no longer has a webhook behind it — that would be taking money
    and recording nothing."""
    monkeypatch.setattr(settings, "tribute_link", "https://t.me/tribute/app?startapp=old")
    monkeypatch.setattr(settings, "tribute_webhook_secret", "still-here")
    assert settings.can_sell is False


def test_support_is_the_fallback_but_sales_wins(monkeypatch):
    """Two different jobs, even when one person does both today. Someone who wants to BUY
    should not land in a support queue."""
    monkeypatch.setattr(settings, "support_contact", "@help")
    monkeypatch.setattr(settings, "sales_contact", "")
    assert settings.sales_handle == "help"
    monkeypatch.setattr(settings, "sales_contact", "@money")
    assert settings.sales_handle == "money"


def test_the_at_sign_is_stripped_once(monkeypatch):
    """It goes into a t.me URL, where a second @ produces a link to nobody."""
    monkeypatch.setattr(settings, "sales_contact", "@iambrock")
    assert settings.sales_handle == "iambrock"
    monkeypatch.setattr(settings, "sales_contact", "iambrock")
    assert settings.sales_handle == "iambrock"


# --- the button --------------------------------------------------------------

def test_the_button_opens_a_chat_not_a_checkout(selling):
    markup = keyboards.plan_actions("en", can_subscribe=True)
    assert markup is not None
    urls = [b.url for row in markup.inline_keyboard for b in row]
    assert urls == ["https://t.me/iambrock"]
    assert not any("tribute" in (u or "").lower() for u in urls)


def test_there_is_exactly_one_button(selling):
    """Three tier buttons made sense when each opened a different checkout. They would now
    be three ways to open the same chat, which reads as a mistake."""
    markup = keyboards.plan_actions("ru", can_subscribe=True)
    assert sum(len(row) for row in markup.inline_keyboard) == 1


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_button_is_translated(selling, lang):
    markup = keyboards.plan_actions(lang, can_subscribe=True)
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels and labels[0].strip()


def test_nobody_who_already_has_premium_is_shown_it(selling):
    """`render.selling` is the one decision, and the keyboard obeys it — this is what
    stopped a channel subscriber being sold what they already had."""
    premium = {**FREE, "premium": True, "premium_via": "channel"}
    assert render.selling(premium, can_subscribe=_can_subscribe()) is False
    assert keyboards.plan_actions("en", can_subscribe=False) is None


# --- the message has to explain what the button does -------------------------

@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_message_names_the_handle(selling, lang):
    """A checkout explained itself; a chat window does not. Somebody who has just read
    three prices and taps Subscribe expects a payment form, and the message is the only
    place that can say otherwise first.

    In the TEXT as well as the button, because a button cannot be copied or forwarded.
    """
    text = render.plan(FREE, lang, can_subscribe=True)
    assert "@iambrock" in text


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_prices_are_still_listed(selling, lang):
    """Direct payment changes how you pay, not what it costs."""
    text = render.plan(FREE, lang, can_subscribe=True)
    cents = TIER_PRICE_CENTS[TIER_FEATURED]
    assert f"{cents // 100}" in text


def test_no_handle_means_no_instruction(monkeypatch):
    monkeypatch.setattr(settings, "sales_contact", "")
    monkeypatch.setattr(settings, "support_contact", "")
    text = render.plan(FREE, "en", can_subscribe=False)
    assert "@" not in text.split("Premium")[0] or "message" not in text.lower()


# --- nothing still tells anyone to cancel a subscription they do not have ----

@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_trial_note_no_longer_promises_a_charge(lang):
    """It said the subscription renews automatically and to cancel in @tribute. A referral
    trial has no card behind it, so that was a false statement about somebody's money —
    and "go and cancel" pointed at a product that is being switched off."""
    data = json.loads((ROOT / f"bot/locales/{lang}.json").read_text(encoding="utf-8"))
    note = data["plan_trial_note"].lower()
    assert "tribute" not in note
    for promise in ("автоматически", "automatically", "automaticamente", "avtomatik"):
        assert promise not in note, f"{lang} still promises an automatic renewal: {note}"
