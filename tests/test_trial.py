"""The trial, which is now Tribute's and not ours.

There used to be an internal 7-day trial granted at first contact: it needed no payment
details, so it worked long before Tribute did. Tribute's own 7-day trial went live on
2026-07-31 and grants access through the purchase webhook — at which point a new user was
getting BOTH. Fourteen days free, and the internal one handed out the product before
anyone was ever asked for a card.

It was not theoretical. The owner's own test account has two `trial_started` events an
hour apart.

So TRIAL_DAYS is 0 and these tests pin that: a new user gets NOTHING automatically, and
the trial arrives only when Tribute says someone has linked a card. That path is covered
in tests/test_tribute_subscriptions.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Purchase, User
from shared.constants import EV_TRIAL_STARTED, TIER_DAYS, TIER_PRICE_CENTS, TRIAL_DAYS


async def test_a_new_user_gets_no_automatic_pass(client, api_db):
    """The change itself. A new account is FREE — all 7106 questions, exam and practice —
    and Premium starts only when Tribute reports a card."""
    r = await client.post("/users", json={"chat_id": 77, "lang": "ru"})
    assert r.status_code == 200
    body = (await client.get("/users/77")).json()
    assert body["has_pass"] is False
    assert body["pass_expires_at"] is None




async def test_a_new_user_cannot_reach_the_paid_features(client, api_db):
    """The other half: with no internal trial, the paywall is what a new user meets."""
    await client.post("/users", json={"chat_id": 78, "lang": "ru"})
    r = await client.post("/users/78/questions/1/translation")
    assert r.json()["translation_state"] in ("locked", "off", "unavailable")




async def test_the_trial_is_granted_once_not_on_every_call(client, api_db):
    """get_or_create is called on /start, on every Mini App auth, and by the webhook.
    Granting on each would be an unlimited trial."""
    first = (await client.post("/users", json={"chat_id": 777, "lang": "ru"})).json()
    for _ in range(3):
        again = (await client.post("/users", json={"chat_id": 777, "lang": "ru"})).json()
    assert again["pass_expires_at"] == first["pass_expires_at"]


async def test_the_trial_is_not_a_purchase(client, api_db):
    """Purchases are money: they drive revenue reporting and are what a refund is matched
    against. Inventing one for a trial would corrupt both."""
    await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    async with api_db() as s:
        purchases = (await s.scalars(select(Purchase))).all()
    assert purchases == []


async def test_no_trial_event_is_written_at_signup(client, api_db):
    """`trial_started` now means one thing only: Tribute reported a card. If signup also
    wrote it, the conversion rate would be measured against a denominator of everyone who
    ever opened the bot."""
    from sqlalchemy import select

    from api.models import Event
    from shared.constants import EV_TRIAL_STARTED

    await client.post("/users", json={"chat_id": 79, "lang": "ru"})
    async with api_db() as session:
        rows = (await session.scalars(
            select(Event).where(Event.type == EV_TRIAL_STARTED)
        )).all()
    assert rows == []


def test_the_internal_trial_stays_off():
    """A stray TRIAL_DAYS=7 would silently restore the double trial, and nothing else in
    the suite would notice."""
    from shared.constants import TRIAL_DAYS

    assert TRIAL_DAYS == 0




async def test_an_expired_trial_locks_the_paid_features(client, api_db):
    """The trial lapses to free rather than auto-charging — see the note in
    shared/constants.py about auto-renewal not being implemented."""
    from tests.conftest import end_trial

    await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    await end_trial(api_db, 777)
    body = (await client.get("/users/777")).json()
    assert body["has_pass"] is False
    assert body["purchased"] is False


# --- pricing ----------------------------------------------------------------

def test_the_three_tiers_match_the_published_prices():
    assert TIER_PRICE_CENTS["pass_1m"] == 299
    assert TIER_PRICE_CENTS["pass_3m"] == 799
    assert TIER_PRICE_CENTS["pass_6m"] == 1099
    assert TIER_DAYS == {"pass_1m": 30, "pass_3m": 90, "pass_6m": 180}


def test_longer_plans_are_cheaper_per_month():
    """If a longer plan ever costs more per month, the pricing table is wrong and the
    'best value' badge would be a lie."""
    rate = {t: TIER_PRICE_CENTS[t] / (TIER_DAYS[t] / 30) for t in TIER_DAYS}
    assert rate["pass_1m"] > rate["pass_3m"] > rate["pass_6m"]


def test_every_tier_has_both_a_price_and_a_length():
    assert set(TIER_DAYS) == set(TIER_PRICE_CENTS)


# --- the product -> tier mapping -------------------------------------------

def test_every_tier_has_a_configurable_product_id(monkeypatch):
    """Adding a tier without wiring its Tribute product id means a real purchase falls
    through to "unrecognised" and grants the SHORTEST pass. The customer pays for six
    months and gets one, and the only symptom is a log line nobody reads."""
    from shared.config import settings

    monkeypatch.setattr(settings, "tribute_product_1m", "prod-1m")
    monkeypatch.setattr(settings, "tribute_product_3m", "prod-3m")
    monkeypatch.setattr(settings, "tribute_product_6m", "prod-6m")

    mapped = set(settings.tribute_products.values())
    assert mapped == set(TIER_DAYS), f"tiers with no product id: {set(TIER_DAYS) - mapped}"


def test_each_product_id_maps_to_its_own_tier(monkeypatch):
    from shared.config import settings
    from api.services.purchases import tier_for

    monkeypatch.setattr(settings, "tribute_product_1m", "prod-1m")
    monkeypatch.setattr(settings, "tribute_product_3m", "prod-3m")
    monkeypatch.setattr(settings, "tribute_product_6m", "prod-6m")

    assert tier_for("prod-1m") == "pass_1m"
    assert tier_for("prod-3m") == "pass_3m"
    assert tier_for("prod-6m") == "pass_6m"


def test_an_unknown_product_grants_the_genuinely_shortest_tier():
    """Erring short under-serves a customer, which is recoverable. But it only errs short
    if the fallback is the smallest by DAYS — TIERS is a tuple whose order is incidental,
    and indexing it would break the moment someone reorders the constants."""
    from api.services.purchases import SHORTEST_TIER, tier_for

    assert TIER_DAYS[SHORTEST_TIER] == min(TIER_DAYS.values())
    assert tier_for("something-nobody-configured") == SHORTEST_TIER
