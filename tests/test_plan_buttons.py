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
