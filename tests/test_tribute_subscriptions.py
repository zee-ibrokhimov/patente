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


# --- periods that are not tiers ---------------------------------------------

@pytest.mark.parametrize("period", ["trial", "onetime", "weekly", "yearly"])
def test_a_known_period_without_a_tier_does_not_warn(period, caplog):
    """`period: "trial"` arrives on EVERY trial start. Falling through to the
    "unrecognised product, defaulting to the shortest tier" warning meant that warning
    fired on the happy path — and a warning that cries wolf is how the real one, a
    genuinely misconfigured product id, gets scrolled past.

    Observed live on the first real Tribute delivery this project ever received.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="api.services.purchases"):
        purchases.parse_event(body("new_subscription", period=period, sub_type=period))
    assert "unrecognised" not in caplog.text.lower()


def test_a_genuinely_unknown_product_still_warns(caplog):
    """The guard above must not silence the case it was built for."""
    import json as _json
    import logging

    raw = _json.dumps({"name": "new_digital_product",
                       "payload": {"telegram_user_id": BUYER, "product_id": "nonsense",
                                   "purchase_id": 555, "amount": 100,
                                   "currency": "eur"}}).encode()
    with caplog.at_level(logging.WARNING, logger="api.services.purchases"):
        purchases.parse_event(raw)
    assert "unrecognised" in caplog.text.lower()


async def test_a_trial_still_takes_its_expiry_from_tribute(client, api_db):
    """The tier is cosmetic for a subscription; the date is what matters."""
    from datetime import datetime, timedelta, timezone

    ends = datetime.now(timezone.utc) + timedelta(days=7)
    await post(client, body("new_subscription", period="trial", sub_type="trial",
                            amount=0, expires=ends))
    async with api_db() as s:
        assert (await s.get(User, BUYER)).pass_expires_at.date() == ends.date()


# --- refunds on a SUBSCRIPTION ----------------------------------------------
#
# Every refund test in this suite used `digital_product_refunded`, which carries a real
# purchase id and matched. This product sells SUBSCRIPTIONS, so every real refund and
# every chargeback arrives as `subscription_refunded` — carrying the bare subscription id,
# with no period. Purchase rows are keyed "<sub_id>:<period_end>" so renewals are not read
# as redeliveries, which meant the refund could never find its row.
#
# It failed silently: "refund for unknown purchase" in the log, the pass untouched, and
# /admin still counting the money because it filters on refunded_at IS NULL.


async def test_a_subscription_refund_finds_its_purchase(client, api_db):
    from sqlalchemy import select

    await post(client, body("new_subscription", sub_id=777))
    r = await post(client, body("subscription_refunded", sub_id=777))
    assert r.json()["status"] == "refunded", "the refund did not match its purchase row"

    async with api_db() as s:
        row = (await s.scalars(select(Purchase).where(Purchase.chat_id == BUYER))).one()
        assert row.refunded_at is not None


async def test_a_refunded_subscription_loses_premium(client, api_db):
    """The point of the whole path, and what the EU withdrawal right requires."""
    from api.services.entitlement import evaluate

    await post(client, body("new_subscription", sub_id=778))
    await post(client, body("subscription_refunded", sub_id=778))
    async with api_db() as s:
        user = await s.get(User, BUYER)
        assert evaluate(user).premium is False


async def test_a_refund_takes_the_newest_period_not_the_oldest(client, api_db):
    """A refund names a subscription, not a month. With several renewals stored under the
    same subscription id, taking back the first month would leave the customer holding the
    time they were just refunded for."""
    from sqlalchemy import select

    early = datetime.now(timezone.utc) + timedelta(days=30)
    late = datetime.now(timezone.utc) + timedelta(days=60)
    await post(client, body("new_subscription", sub_id=779, expires=early))
    await post(client, body("renewed_subscription", sub_id=779, expires=late))
    await post(client, body("subscription_refunded", sub_id=779))

    async with api_db() as s:
        rows = (await s.scalars(
            select(Purchase).where(Purchase.chat_id == BUYER).order_by(Purchase.id)
        )).all()
    assert rows[0].refunded_at is None, "the first period should still stand"
    assert rows[1].refunded_at is not None, "the newest period is the one refunded"


async def test_refunding_twice_is_not_counted_twice(client, api_db):
    raw = body("subscription_refunded", sub_id=780)
    await post(client, body("new_subscription", sub_id=780))
    await post(client, raw)
    r = await post(client, raw)
    assert r.json()["status"] in ("duplicate", "already-refunded", "unknown")


# --- a refund must revoke every source of Premium ---------------------------

async def test_a_refund_rechecks_channel_membership(client, api_db, monkeypatch):
    """Premium has three sources and a refund revoked only one.

    Tribute adds buyers to a channel, and membership grants Premium on its own — put
    there so a webhook we never received cannot lock out someone who paid. The reverse
    was never handled: a refunded buyer stayed a member, so `via_channel` kept them
    Premium for ever. Their money back AND the product.
    """
    from api.services import channel as channel_service

    checked = []

    async def gone(chat_id):
        checked.append(chat_id)
        return "left"

    monkeypatch.setattr(channel_service, "fetch_status", gone)
    monkeypatch.setattr(settings, "premium_channel_id", "-100999")

    await post(client, body("new_subscription", sub_id=901))
    await post(client, body("subscription_refunded", sub_id=901))

    assert BUYER in checked, "the refund never re-checked channel membership"
    async with api_db() as s:
        from api.services.entitlement import evaluate
        assert evaluate(await s.get(User, BUYER)).premium is False


async def test_a_refunded_member_tribute_did_not_eject_is_logged(client, api_db, monkeypatch, caplog):
    """No code here can remove someone from a channel we do not own, so the honest
    outcome is a loud log line rather than a silent free ride."""
    import logging

    from api.services import channel as channel_service

    async def still_there(chat_id):
        return "member"

    monkeypatch.setattr(channel_service, "fetch_status", still_there)
    monkeypatch.setattr(settings, "premium_channel_id", "-100999")

    await post(client, body("new_subscription", sub_id=902))
    with caplog.at_level(logging.ERROR, logger="api.services.purchases"):
        await post(client, body("subscription_refunded", sub_id=902))
    assert "STILL in the Premium channel" in caplog.text


# --- a payload we cannot read must not be generous --------------------------

async def test_a_trial_with_an_unreadable_date_grants_seven_days_not_thirty(client, api_db):
    """Tribute's `expires_at` is authoritative and this should never happen. When it did,
    the code fell through to TIER_DAYS — and a trial has no tier, so it took the shortest
    PAID one: thirty days free on the strength of a payload we could not parse."""
    import json as _json

    raw = _json.dumps({
        "name": "new_subscription",
        "payload": {"subscription_id": 950, "telegram_user_id": BUYER, "amount": 0,
                    "currency": "eur", "expires_at": "not-a-date",
                    "type": "trial", "period": "trial"},
    }).encode()
    await post(client, raw)

    async with api_db() as s:
        user = await s.get(User, BUYER)
    days = (user.pass_expires_at - datetime.now(timezone.utc)).days
    assert 5 <= days <= 8, f"granted {days} days for an unreadable trial date"


async def test_a_paid_subscription_with_no_date_still_gets_its_tier(client, api_db):
    """The narrowing applies to TRIALS only. Someone who actually paid for a month is
    owed a month, whatever the payload's date field did."""
    import json as _json

    raw = _json.dumps({
        "name": "new_subscription",
        "payload": {"subscription_id": 951, "telegram_user_id": BUYER, "amount": 299,
                    "currency": "eur", "expires_at": "", "type": "regular",
                    "period": "monthly"},
    }).encode()
    await post(client, raw)

    async with api_db() as s:
        user = await s.get(User, BUYER)
    days = (user.pass_expires_at - datetime.now(timezone.utc)).days
    assert days >= 28, f"a paid month granted only {days} days"


async def test_an_unreadable_trial_date_is_logged_loudly(client, caplog):
    """A payment provider sending a date we cannot read is worth someone looking at."""
    import json as _json
    import logging

    raw = _json.dumps({
        "name": "new_subscription",
        "payload": {"subscription_id": 952, "telegram_user_id": BUYER, "amount": 0,
                    "currency": "eur", "expires_at": "nonsense",
                    "type": "trial", "period": "trial"},
    }).encode()
    with caplog.at_level(logging.ERROR, logger="api.services.purchases"):
        await post(client, raw)
    assert "no usable expires_at" in caplog.text


# --- a refund puts the pass back exactly where it was -----------------------

async def test_a_refund_restores_the_exact_previous_expiry(client, api_db):
    """It used to subtract TIER_DAYS[tier] — our idea of how long a tier lasts. But a
    subscription grants TRIBUTE's expires_at, which is a real billing period and not
    exactly 30 days. Revoking a 31-day month took 30 and left a free day; a short first
    period had two days taken that were never given."""
    from sqlalchemy import update as sa_update

    had = datetime.now(timezone.utc) + timedelta(days=5)
    async with api_db() as s:
        s.add(User(chat_id=BUYER, lang="ru", pass_expires_at=had))
        await s.commit()

    # A 31-day month, which no TIER_DAYS value matches.
    await post(client, body("new_subscription", sub_id=960,
                            expires=had + timedelta(days=31)))
    await post(client, body("subscription_refunded", sub_id=960))

    async with api_db() as s:
        user = await s.get(User, BUYER)
    drift = abs((user.pass_expires_at - had).total_seconds())
    assert drift < 5, f"the pass came back {drift/86400:.2f} days away from where it was"


async def test_a_refund_never_hands_back_time_still_owned(client, api_db):
    """A LATER purchase may have pushed the expiry beyond what this one set. Restoring a
    stale previous value would give away time the customer still has."""
    async with api_db() as s:
        s.add(User(chat_id=BUYER, lang="ru", pass_expires_at=None))
        await s.commit()

    first = datetime.now(timezone.utc) + timedelta(days=30)
    second = datetime.now(timezone.utc) + timedelta(days=120)
    await post(client, body("new_subscription", sub_id=961, expires=first))
    await post(client, body("new_subscription", sub_id=962, expires=second))
    await post(client, body("subscription_refunded", sub_id=961))

    async with api_db() as s:
        user = await s.get(User, BUYER)
    assert user.pass_expires_at <= second, "a refund extended the pass"


async def test_a_refund_of_the_only_purchase_ends_the_pass(client, api_db):
    """Someone with no prior pass had nothing to restore, so the withdrawal is immediate."""
    from api.services.entitlement import evaluate

    await post(client, body("new_subscription", sub_id=963))
    await post(client, body("subscription_refunded", sub_id=963))
    async with api_db() as s:
        assert evaluate(await s.get(User, BUYER)).premium is False
