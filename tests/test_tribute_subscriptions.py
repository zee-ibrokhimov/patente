"""Tribute subscription webhooks, built from their published payloads.

Every body here is shaped like the example in Tribute's OpenAPI spec — top-level `name`
in snake_case, the interesting fields nested under `payload`, `expires_at` in RFC3339 with
a Z. That matters more than usual: the previous version of this integration was written
from the plan's *description* of the webhook rather than from the schema, and two events
were missing entirely.

`renewed_subscription` and `cancelled_subscription` were not merely unhandled — they were
REJECTED, because parse_event raises on an unrecognised name and the route turns that into
a 400. Tribute retries a 400 forever. So a monthly subscriber's second payment would have
granted nothing while the delivery kept coming back, and the only visible symptom would
have been a customer saying they lost access they had paid for.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Purchase, User
from api.services import purchases
from shared.config import settings
from shared.constants import EV_SUBSCRIPTION_CANCELLED, EV_TRIAL_STARTED, TIER_1M, TIER_3M

SECRET = "test-webhook-secret-not-real"
BUYER = 4242


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "tribute_webhook_secret", SECRET)


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def body(name: str, *, sub_id: int = 1644, expires: datetime | None = None,
         sub_type: str = "regular", amount: int = 700, period: str = "monthly",
         chat_id: int = BUYER) -> bytes:
    """A payload shaped exactly like Tribute's documented example."""
    expires = expires or datetime.now(timezone.utc) + timedelta(days=30)
    return json.dumps({
        "name": name,
        "created_at": "2026-07-31T01:15:58.33246Z",
        "sent_at": "2026-07-31T01:15:58.542279448Z",
        "payload": {
            "subscription_id": sub_id,
            "telegram_user_id": chat_id,
            "amount": amount,
            "currency": "eur",
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
            "type": sub_type,
            "period": period,
        },
    }).encode()


async def post(client, raw: bytes):
    return await client.post("/webhooks/tribute", content=raw,
                             headers={purchases.SIGNATURE_HEADER: sign(raw)})


# --- the two events that used to be rejected --------------------------------

@pytest.mark.parametrize("name", ["renewed_subscription", "cancelled_subscription"])
def test_the_missing_events_now_parse_at_all(name):
    """The regression in its simplest form: these raised WebhookRejected."""
    event = purchases.parse_event(body(name))
    assert event.kind in ("renewal", "cancellation")


@pytest.mark.parametrize("name, kind", [
    ("new_subscription", "purchase"),
    ("renewed_subscription", "renewal"),
    ("cancelled_subscription", "cancellation"),
    ("new_digital_product", "purchase"),
    ("digital_product_refunded", "refund"),
])
def test_every_real_tribute_event_is_classified(name, kind):
    assert purchases.parse_event(body(name)).kind == kind


def test_an_unknown_event_is_still_refused():
    """Loosening the matcher must not turn it into "accept anything"."""
    with pytest.raises(purchases.WebhookRejected):
        purchases.parse_event(body("physical_order_shipped"))


# --- renewal ----------------------------------------------------------------

async def test_a_renewal_extends_the_pass(client, api_db):
    later = datetime.now(timezone.utc) + timedelta(days=60)
    r = await post(client, body("new_subscription"))
    assert r.status_code == 200
    r = await post(client, body("renewed_subscription", expires=later))
    assert r.status_code == 200
    assert r.json()["status"] == "renewed"

    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert user.pass_expires_at.date() == later.date()


async def test_a_renewal_is_not_mistaken_for_a_redelivery(client, api_db):
    """Every renewal of one subscription carries the SAME subscription_id. Keying
    idempotency on that alone would make month two look like a duplicate of month one
    and silently grant nothing — the customer pays and loses access."""
    first = datetime.now(timezone.utc) + timedelta(days=30)
    second = datetime.now(timezone.utc) + timedelta(days=60)
    await post(client, body("new_subscription", sub_id=99, expires=first))
    r = await post(client, body("renewed_subscription", sub_id=99, expires=second))
    assert r.json()["status"] == "renewed"

    async with api_db() as s:
        rows = (await s.scalars(select(Purchase).where(Purchase.chat_id == BUYER))).all()
        assert len(rows) == 2, "the renewal was swallowed as a duplicate"


async def test_a_genuine_redelivery_is_still_ignored(client, api_db):
    raw = body("renewed_subscription", sub_id=7)
    await post(client, raw)
    r = await post(client, raw)
    assert r.json()["status"] == "duplicate"


async def test_tribute_owns_the_expiry_date(client, api_db):
    """Their clock, not ours. Two systems computing the same date from different inputs
    drift, and the one holding the card should win."""
    theirs = datetime.now(timezone.utc) + timedelta(days=93)
    await post(client, body("new_subscription", expires=theirs, period="quarterly"))
    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert abs((user.pass_expires_at - theirs).total_seconds()) < 2


async def test_a_renewal_never_shortens_a_longer_pass(client, api_db):
    """Someone who also bought a one-off six-month pass must not have it truncated by a
    monthly renewal that lands earlier."""
    async with api_db() as s:
        s.add(User(chat_id=BUYER, lang="ru",
                   pass_expires_at=datetime.now(timezone.utc) + timedelta(days=180)))
        await s.commit()
    await post(client, body("renewed_subscription",
                            expires=datetime.now(timezone.utc) + timedelta(days=30)))
    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert user.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=170)


# --- cancellation -----------------------------------------------------------

async def test_cancelling_does_not_take_away_paid_time(client, api_db):
    """The load-bearing one. Cancel means "do not bill me again", not "cut me off now".
    Revoking here removes time the customer has already bought."""
    await post(client, body("new_subscription"))
    async with api_db() as s:
        before = (await s.get(User, BUYER)).pass_expires_at

    r = await post(client, body("cancelled_subscription"))
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    async with api_db() as s:
        assert (await s.get(User, BUYER)).pass_expires_at == before


async def test_a_cancellation_is_recorded(client, api_db):
    """Otherwise churn is invisible — the only trace would be a renewal that never came."""
    await post(client, body("new_subscription"))
    await post(client, body("cancelled_subscription"))
    async with api_db() as s:
        names = [e.type for e in (await s.scalars(select(Event))).all()]
        assert EV_SUBSCRIPTION_CANCELLED in names


async def test_cancelling_for_an_unknown_user_is_not_an_error(client):
    """A 4xx would make Tribute retry a delivery that can never succeed."""
    r = await post(client, body("cancelled_subscription", chat_id=999999))
    assert r.status_code == 200
    assert r.json()["status"] == "unknown-user"


# --- the trial --------------------------------------------------------------

async def test_a_trial_grants_access(client, api_db):
    ends = datetime.now(timezone.utc) + timedelta(days=7)
    r = await post(client, body("new_subscription", sub_type="trial", amount=0,
                                expires=ends))
    assert r.json()["status"] == "trial"
    async with api_db() as s:
        assert (await s.get(User, BUYER)).pass_expires_at.date() == ends.date()


async def test_a_trial_does_not_read_as_a_paying_customer(client, api_db):
    """A trial writes a Purchase row — that row's UNIQUE id is what makes redelivery
    idempotent — but at zero. If `purchased` counted rows instead of money, /plan would
    stop selling to precisely the people the trial exists to sell to."""
    await post(client, body("new_subscription", sub_type="trial", amount=0))
    r = await client.get(f"/users/{BUYER}")
    assert r.json()["has_pass"] is True
    assert r.json()["purchased"] is False


async def test_a_trial_is_stored_at_zero_not_at_the_list_price(client, api_db):
    await post(client, body("new_subscription", sub_type="trial", amount=0))
    async with api_db() as s:
        row = (await s.scalars(select(Purchase).where(Purchase.chat_id == BUYER))).one()
        assert row.amount_cents == 0


async def test_a_trial_is_logged_as_a_trial_not_a_purchase(client, api_db):
    await post(client, body("new_subscription", sub_type="trial", amount=0))
    async with api_db() as s:
        names = [e.type for e in (await s.scalars(select(Event))).all()]
        assert EV_TRIAL_STARTED in names


async def test_converting_from_trial_to_paid_makes_them_a_customer(client, api_db):
    """Tribute's documented behaviour: a trial that renews becomes `regular`. That is the
    conversion, and it is the moment the app must stop treating them as a trialist."""
    await post(client, body("new_subscription", sub_id=55, sub_type="trial", amount=0))
    assert (await client.get(f"/users/{BUYER}")).json()["purchased"] is False

    await post(client, body("renewed_subscription", sub_id=55, sub_type="regular",
                            amount=799,
                            expires=datetime.now(timezone.utc) + timedelta(days=90)))
    assert (await client.get(f"/users/{BUYER}")).json()["purchased"] is True


# --- the guarantees that must survive all of this ---------------------------

async def test_an_unsigned_delivery_is_still_refused(client):
    raw = body("renewed_subscription")
    r = await client.post("/webhooks/tribute", content=raw)
    assert r.status_code == 400


async def test_a_forged_signature_is_still_refused(client):
    raw = body("cancelled_subscription")
    r = await client.post("/webhooks/tribute", content=raw,
                          headers={purchases.SIGNATURE_HEADER: "00" * 32})
    assert r.status_code == 400
