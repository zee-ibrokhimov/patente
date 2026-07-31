"""The 7-day free trial.

Every new user gets full Premium for a week at first contact. This replaced the
three-explanation taster: three explanations sampled one feature, a week samples the
product.

The tests that matter most are about what a trial is NOT — it is not a purchase, and it
must not look like one, or the conversion number the pricing decision rests on becomes
uninterpretable and cannot be recomputed later.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Purchase, User
from shared.constants import EV_TRIAL_STARTED, TIER_DAYS, TIER_PRICE_CENTS, TRIAL_DAYS


async def test_a_new_user_starts_the_trial_with_full_access(client, api_db):
    r = await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    assert r.status_code == 200
    body = r.json()
    assert body["has_pass"] is True, "a new user should be on the trial"
    assert body["purchased"] is False, "and must not look like a paying customer"

    expires = datetime.fromisoformat(body["pass_expires_at"].replace("Z", "+00:00"))
    days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
    assert TRIAL_DAYS - 0.1 < days <= TRIAL_DAYS


async def test_the_trial_unlocks_translations(client, api_db):
    """The point of a trial: the paid features actually work during it."""
    await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    body = (await client.get("/users/777/next-question")).json()
    assert body["translation_state"] != "locked"


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


async def test_the_trial_is_a_distinct_event(client, api_db):
    """'Converted after a trial' is the number that decides whether the trial works, and
    it is only separable if trials and purchases are different events from the first user.
    Events cannot be backfilled."""
    await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    async with api_db() as s:
        events = (await s.scalars(
            select(Event).where(Event.type == EV_TRIAL_STARTED)
        )).all()
    assert len(events) == 1
    assert events[0].chat_id == 777
    assert events[0].payload["days"] == TRIAL_DAYS


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
