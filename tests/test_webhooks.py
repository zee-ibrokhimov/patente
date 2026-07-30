"""Tribute webhooks. Plan §14.1 names webhook idempotency as one of three things the
suite exists to defend, alongside entitlement and Leitner.

What is defended here:

  · an unsigned or wrongly-signed delivery never grants anything, and the absence of a
    configured secret stops everything rather than skipping the check
  · a redelivered payment does not extend a pass twice, and answers 200 so Tribute stops
    retrying a webhook that is in fact working
  · a refund takes back what that purchase granted and no more, so someone who bought
    twice and had one refunded keeps what they still paid for
  · the signature is checked against the bytes that were sent, not against a re-serialised
    copy of them

The payload field names are written from the plan rather than a real delivery (the
credentials are still outstanding), so these fix the *structure*, not the spelling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Purchase, User
from api.services import purchases
from shared.config import settings
from shared.constants import EV_PURCHASE_COMPLETED, EV_PURCHASE_REFUNDED, TIER_DAYS, TIERS

SECRET = "test-webhook-secret"
BUYER = 42


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setattr(settings, "tribute_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "tribute_product_1m", "prod_1m")
    monkeypatch.setattr(settings, "tribute_product_3m", "prod_3m")


def body_for(name="digital_product_purchased", purchase_id="pay_1",
             chat_id=BUYER, product="prod_1m", amount=299) -> bytes:
    return json.dumps({
        "name": name,
        "payload": {
            "purchase_id": purchase_id,
            "telegram_user_id": chat_id,
            "product_id": product,
            "amount": amount,
            "currency": "EUR",
        },
    }).encode("utf-8")


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def post(client, body: bytes, signature: str | None = "auto"):
    headers = {}
    if signature == "auto":
        signature = sign(body)
    if signature is not None:
        headers[purchases.SIGNATURE_HEADER] = signature
    return await client.post("/webhooks/tribute", content=body, headers=headers)


async def events_of(api_db, event_type):
    from api.models import Event

    async with api_db() as s:
        return (await s.scalars(select(Event).where(Event.type == event_type))).all()


# --- the signature ----------------------------------------------------------

async def test_a_valid_signature_is_accepted(client, registered):
    assert (await post(client, body_for())).status_code == 200


async def test_a_wrong_signature_grants_nothing(client, registered, api_db):
    response = await post(client, body_for(), signature=sign(body_for(), "wrong-secret"))
    assert response.status_code == 400
    async with api_db() as s:
        assert (await s.scalars(select(Purchase))).all() == []
        assert (await s.get(User, BUYER)).pass_expires_at is None


async def test_a_missing_signature_grants_nothing(client, registered):
    assert (await post(client, body_for(), signature=None)).status_code == 400


async def test_no_configured_secret_fails_closed(client, registered, monkeypatch):
    """An unsigned webhook that grants a paid pass hands the product to anyone who can
    guess the URL, so a missing secret must stop everything rather than skip the check."""
    monkeypatch.setattr(settings, "tribute_webhook_secret", "")
    assert (await post(client, body_for())).status_code == 400


async def test_the_signature_covers_the_bytes_that_were_sent(client, registered):
    """Signing a re-serialised copy must fail. This is why the route takes raw bytes: JSON
    round-tripping changes key order and spacing, and the HMAC covers the original."""
    original = body_for()
    reserialised = json.dumps(json.loads(original), indent=2, sort_keys=True).encode()
    assert reserialised != original
    assert (await post(client, original, signature=sign(reserialised))).status_code == 400


async def test_a_tampered_amount_is_rejected(client, registered):
    """The classic attack: replay a real delivery with the numbers changed."""
    authentic = body_for(amount=299)
    tampered = body_for(amount=1)
    assert (await post(client, tampered, signature=sign(authentic))).status_code == 400


# --- idempotency ------------------------------------------------------------

async def test_a_purchase_extends_the_pass(client, registered, api_db):
    assert (await post(client, body_for())).json()["status"] == "applied"
    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert user.pass_expires_at is not None
        expected = datetime.now(timezone.utc) + timedelta(days=TIER_DAYS[TIERS[0]])
        assert abs((user.pass_expires_at - expected).total_seconds()) < 60


async def test_redelivery_does_not_extend_twice(client, registered, api_db):
    """Webhook redelivery is normal — a timeout on our side, a retry on theirs."""
    await post(client, body_for(purchase_id="pay_1"))
    async with api_db() as s:
        after_first = (await s.get(User, BUYER)).pass_expires_at

    second = await post(client, body_for(purchase_id="pay_1"))
    assert second.status_code == 200, "a duplicate is the system working, not an error"
    assert second.json()["status"] == "duplicate"

    async with api_db() as s:
        assert (await s.get(User, BUYER)).pass_expires_at == after_first
        assert len((await s.scalars(select(Purchase))).all()) == 1


async def test_a_duplicate_answers_200_so_tribute_stops_retrying(client, registered):
    await post(client, body_for(purchase_id="pay_x"))
    again = await post(client, body_for(purchase_id="pay_x"))
    assert again.status_code == 200


async def test_two_different_purchases_stack(client, registered, api_db):
    """Buying twice adds time. Extending from today instead would silently shorten the
    pass of anyone who renewed early."""
    await post(client, body_for(purchase_id="pay_1"))
    async with api_db() as s:
        after_first = (await s.get(User, BUYER)).pass_expires_at

    await post(client, body_for(purchase_id="pay_2"))
    async with api_db() as s:
        after_second = (await s.get(User, BUYER)).pass_expires_at

    added = (after_second - after_first).days
    assert added == TIER_DAYS[TIERS[0]]


async def test_a_purchase_is_logged_for_the_conversion_funnel(client, registered, api_db):
    await post(client, body_for(purchase_id="pay_1"))
    logged = await events_of(api_db, EV_PURCHASE_COMPLETED)
    assert len(logged) == 1
    assert logged[0].payload["purchase_id"] == "pay_1"


async def test_paying_before_ever_starting_the_bot_still_credits_them(client, api_db):
    """Losing a payment is far worse than an unexpected user row, and /start is idempotent
    so it adopts this one."""
    response = await post(client, body_for(chat_id=777))
    assert response.status_code == 200
    async with api_db() as s:
        user = await s.get(User, 777)
        assert user is not None and user.pass_expires_at is not None


# --- refunds, and the EU withdrawal right -----------------------------------

async def test_a_refund_revokes_the_pass(client, registered, api_db):
    await post(client, body_for(purchase_id="pay_1"))
    response = await post(client, body_for(name="digital_product_refunded",
                                          purchase_id="pay_1"))
    assert response.json()["status"] == "refunded"

    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert user.pass_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=5)
        purchase = await s.scalar(
            select(Purchase).where(Purchase.tribute_purchase_id == "pay_1")
        )
        assert purchase.refunded_at is not None


async def test_a_refund_takes_back_only_what_that_purchase_granted(client, registered, api_db):
    """Bought twice, one refunded: they keep what they still paid for."""
    await post(client, body_for(purchase_id="pay_1"))
    await post(client, body_for(purchase_id="pay_2"))
    await post(client, body_for(name="digital_product_refunded", purchase_id="pay_1"))

    async with api_db() as s:
        user = await s.get(User, BUYER)
    remaining = (user.pass_expires_at - datetime.now(timezone.utc)).days
    assert remaining >= TIER_DAYS[TIERS[0]] - 1, "the un-refunded purchase was revoked too"


async def test_a_refund_is_not_applied_twice(client, registered, api_db):
    await post(client, body_for(purchase_id="pay_1"))
    await post(client, body_for(purchase_id="pay_2"))
    await post(client, body_for(name="digital_product_refunded", purchase_id="pay_1"))
    async with api_db() as s:
        after_first = (await s.get(User, BUYER)).pass_expires_at

    again = await post(client, body_for(name="digital_product_refunded",
                                       purchase_id="pay_1"))
    assert again.json()["status"] == "duplicate"
    async with api_db() as s:
        assert (await s.get(User, BUYER)).pass_expires_at == after_first


async def test_a_refund_for_an_unknown_purchase_is_reported_not_retried(client, registered):
    """There is nothing to revoke, so retrying will not help — but it means a purchase
    webhook was missed, which is worth shouting about in the log."""
    response = await post(client, body_for(name="digital_product_refunded",
                                          purchase_id="never_seen"))
    assert response.status_code == 200
    assert response.json()["status"] == "unknown"


async def test_a_refund_is_logged(client, registered, api_db):
    await post(client, body_for(purchase_id="pay_1"))
    await post(client, body_for(name="digital_product_refunded", purchase_id="pay_1"))
    assert len(await events_of(api_db, EV_PURCHASE_REFUNDED)) == 1


# --- parsing ----------------------------------------------------------------

async def test_an_unparseable_body_is_a_400_not_a_500(client, registered):
    """It must not look like our failure, or Tribute will retry it forever."""
    body = b"not json at all"
    assert (await post(client, body)).status_code == 400


async def test_an_unrecognised_event_name_is_refused(client, registered):
    assert (await post(client, body_for(name="account_updated"))).status_code == 400


async def test_a_purchase_with_no_telegram_id_is_refused(client, registered):
    """Nobody to credit. Better a visible rejection than money quietly going nowhere."""
    assert (await post(client, body_for(chat_id=None))).status_code == 400


def test_the_three_month_product_maps_to_the_longer_tier(secret):
    assert purchases.tier_for("prod_3m") == TIERS[1]
    assert purchases.tier_for("prod_1m") == TIERS[0]


def test_an_unknown_product_falls_back_to_the_shortest_tier(secret):
    """A payment we cannot classify is still a payment. Erring short under-serves the
    customer, which is recoverable; refusing it keeps their money for nothing."""
    assert purchases.tier_for("something_new") == TIERS[0]


def test_extend_stacks_from_the_later_date():
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    active = now + timedelta(days=10)
    assert purchases.extend(active, 30, now) == active + timedelta(days=30)
    assert purchases.extend(now - timedelta(days=5), 30, now) == now + timedelta(days=30)
    assert purchases.extend(None, 30, now) == now + timedelta(days=30)
