"""The Buy buttons: what makes them appear, and what they open.

The gate used to be `tribute_webhook_secret and tribute_product_1m`. A product id belongs
to a one-off DIGITAL PRODUCT; a subscription payload carries none at all, because the tier
comes from `period`. So a subscription-based setup — the model the owner asked for, with a
trial that auto-converts — could never have opened the gate, and /plan would have gone on
saying "payments are not connected" with Tribute fully configured behind it.

The right test is the checkout link, since that is exactly what a button needs in order to
lead somewhere.
"""

from __future__ import annotations

import pytest

from bot import keyboards
from bot.handlers.progress import _can_subscribe
from shared.config import settings
from shared.constants import TIER_FEATURED, TIER_PRICE_CENTS

LINKS = {
    "tribute_link_1m": "https://t.me/tribute/app?startapp=one",
    "tribute_link_3m": "https://t.me/tribute/app?startapp=three",
    "tribute_link_6m": "https://t.me/tribute/app?startapp=six",
}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    for field, url in LINKS.items():
        monkeypatch.setattr(settings, field, url)


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# --- the gate ---------------------------------------------------------------

def test_no_links_means_no_buttons(monkeypatch):
    """A Buy button that opens nothing reads as a broken product rather than an
    unfinished one."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    for field in LINKS:
        monkeypatch.setattr(settings, field, "")
    assert not _can_subscribe()
    assert keyboards.plan_actions("ru", can_subscribe=True) is None


def test_links_without_a_webhook_secret_are_not_enough(monkeypatch):
    """Selling before the webhook can be verified means taking money and granting
    nothing — verify() fails closed without the secret, so every delivery would 400."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "")
    for field, url in LINKS.items():
        monkeypatch.setattr(settings, field, url)
    assert not _can_subscribe()


def test_a_product_id_is_no_longer_required(monkeypatch):
    """The regression this file exists for. A subscription has no product id."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    monkeypatch.setattr(settings, "tribute_product_1m", "")
    for field, url in LINKS.items():
        monkeypatch.setattr(settings, field, url)
    assert _can_subscribe(), "a subscription-only setup must be able to sell"


def test_one_configured_tier_is_enough(monkeypatch):
    """Selling only the 3-month plan is a legitimate choice — it is how the owner would
    restrict the trial to that tier."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    monkeypatch.setattr(settings, "tribute_link_1m", "")
    monkeypatch.setattr(settings, "tribute_link_6m", "")
    monkeypatch.setattr(settings, "tribute_link_3m", LINKS["tribute_link_3m"])
    assert _can_subscribe()
    assert len(buttons(keyboards.plan_actions("ru", can_subscribe=True))) == 1


# --- what the buttons say and do -------------------------------------------

def test_one_button_per_configured_tier(configured):
    assert len(buttons(keyboards.plan_actions("ru", can_subscribe=True))) == 3


def test_every_button_opens_its_own_checkout(configured):
    urls = [b.url for b in buttons(keyboards.plan_actions("ru", can_subscribe=True))]
    assert urls == list(LINKS.values())
    assert all(u for u in urls), "a button with no url would do nothing when tapped"


def test_buttons_run_shortest_to_longest(configured):
    """The same order as the price list in the message. Two different orders would make
    the reader check each one against the other."""
    texts = [b.text for b in buttons(keyboards.plan_actions("ru", can_subscribe=True))]
    assert "1" in texts[0] and "3" in texts[1] and "6" in texts[2]


def test_the_featured_tier_is_marked(configured):
    texts = [b.text for b in buttons(keyboards.plan_actions("ru", can_subscribe=True))]
    starred = [t for t in texts if "⭐" in t]
    assert len(starred) == 1
    price = TIER_PRICE_CENTS[TIER_FEATURED]
    assert f"{price // 100}.{price % 100:02d}" in starred[0]


@pytest.mark.parametrize("lang", ["it", "ru", "en", "uz"])
def test_the_buttons_are_translated(configured, lang):
    texts = [b.text for b in buttons(keyboards.plan_actions(lang, can_subscribe=True))]
    assert all(t.strip() for t in texts)
    # A missing key renders as the key itself, which is how a locale silently ships raw
    # identifiers to users.
    assert not any("plan_" in t for t in texts)


def test_the_price_on_the_button_matches_the_constant(configured):
    """The button and the message must never disagree about the price — that reads as a
    trick, and it is the one number nobody forgives being wrong."""
    texts = [b.text for b in buttons(keyboards.plan_actions("en", can_subscribe=True))]
    for tier, cents in TIER_PRICE_CENTS.items():
        wanted = f"{cents // 100}.{cents % 100:02d}"
        assert any(wanted in t for t in texts), f"no button shows {wanted}"


# --- the shape Tribute actually produces ------------------------------------
#
# One subscription carrying every period, so ONE link. The buyer chooses the period on
# Tribute's own page. The per-tier settings above cover the other shape — separate
# products, each with its own URL — and are unused when this one is set.


@pytest.fixture
def single_link(monkeypatch):
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    monkeypatch.setattr(settings, "tribute_link", "https://t.me/tribute/app?startapp=s12aI")
    for field in LINKS:
        monkeypatch.setattr(settings, field, "")


def test_one_link_gives_exactly_one_button(single_link):
    """Three buttons pointing at the same page would be three ways to reach one screen,
    which reads as a mistake rather than a choice — and /plan has already listed the
    prices immediately above."""
    b = buttons(keyboards.plan_actions("ru", can_subscribe=True))
    assert len(b) == 1
    assert b[0].url == "https://t.me/tribute/app?startapp=s12aI"


def test_the_single_link_opens_the_gate(single_link):
    assert _can_subscribe()


def test_the_single_link_wins_over_per_tier_links(monkeypatch, single_link):
    """Both configured is a misconfiguration, not a feature. One button is the safe
    reading: it can only ever send someone to the subscription Tribute really has."""
    for field, url in LINKS.items():
        monkeypatch.setattr(settings, field, url)
    assert len(buttons(keyboards.plan_actions("ru", can_subscribe=True))) == 1


def test_a_link_without_a_secret_still_cannot_sell(monkeypatch, single_link):
    monkeypatch.setattr(settings, "tribute_webhook_secret", "")
    assert not _can_subscribe()


@pytest.mark.parametrize("lang", ["it", "ru", "en", "uz"])
def test_the_single_button_is_translated(single_link, lang):
    text = buttons(keyboards.plan_actions(lang, can_subscribe=True))[0].text
    assert text.strip() and "btn_" not in text


# --- one subscription per language ------------------------------------------
#
# A Tribute subscription carries its own name and description, so selling to a Russian
# speaker and an Italian one from the same object means one of them reads the pitch in a
# language they did not choose, at the moment they are deciding whether to pay.
#
# The owner is creating them one language at a time, so the missing ones have to degrade
# to something that still sells rather than to no button at all.


@pytest.fixture
def russian_only(monkeypatch):
    """Today: the Russian subscription exists and the others do not."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    monkeypatch.setattr(settings, "tribute_link", "https://t.me/tribute/app?startapp=s12aI")
    for lang in ("ru", "it", "en", "uz"):
        monkeypatch.setattr(settings, f"tribute_link_{lang}", "")
    for field in LINKS:
        monkeypatch.setattr(settings, field, "")


@pytest.mark.parametrize("lang", ["ru", "it", "en", "uz"])
def test_a_missing_language_still_gets_a_working_button(russian_only, lang):
    """Falling back to the default is right and falling back to NOTHING is not: an
    Italian speaker seeing no way to pay is a lost sale, while an Italian speaker seeing
    a Russian checkout page is merely an awkward one."""
    b = buttons(keyboards.plan_actions(lang, can_subscribe=True))
    assert len(b) == 1
    assert b[0].url == "https://t.me/tribute/app?startapp=s12aI"


def test_a_language_link_overrides_the_default(monkeypatch, russian_only):
    """Adding the Italian subscription later must route Italian users to it, without a
    code change."""
    monkeypatch.setattr(settings, "tribute_link_it", "https://t.me/tribute/app?startapp=sIT")
    assert settings.checkout_url("it") == "https://t.me/tribute/app?startapp=sIT"
    assert settings.checkout_url("ru") == "https://t.me/tribute/app?startapp=s12aI"


def test_a_language_link_alone_is_enough_to_sell(monkeypatch):
    """No default configured, only per-language ones. The gate must still open."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "secret")
    monkeypatch.setattr(settings, "tribute_link", "")
    for lang in ("it", "en", "uz"):
        monkeypatch.setattr(settings, f"tribute_link_{lang}", "")
    monkeypatch.setattr(settings, "tribute_link_ru", "https://t.me/tribute/app?startapp=sRU")
    assert _can_subscribe()
    assert buttons(keyboards.plan_actions("ru", can_subscribe=True))[0].url.endswith("sRU")


def test_whitespace_is_not_a_link(monkeypatch, russian_only):
    """A variable set to a blank string in Coolify is easy to do and would otherwise
    route every Italian user to a button that opens nothing."""
    monkeypatch.setattr(settings, "tribute_link_it", "   ")
    assert settings.checkout_url("it") == "https://t.me/tribute/app?startapp=s12aI"
