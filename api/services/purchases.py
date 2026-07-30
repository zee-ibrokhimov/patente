"""Tribute webhooks: money arriving, and money going back.

Build step 9 (plan §4.1). Tribute POSTs here when someone buys a pass and again if the
payment is later refunded — by the App Store, Google Play, or a bank. The refund path is
built at the same time as the purchase path deliberately: §4.1 calls it out because it is
also how the **EU right of withdrawal** is honoured, and a paywall that can take money but
not give it back is a legal problem rather than a missing feature.

THREE THINGS THIS HAS TO GET RIGHT
----------------------------------
**1. Verify the signature over the raw bytes.** The HMAC covers exactly what was sent.
Parsing the JSON and re-serialising it produces different bytes — different key order,
different spacing — and the signature will not match, or worse, will match something the
sender did not say. So the route hands over `bytes` and the parsing happens after the
signature is confirmed, never before.

**2. Be idempotent.** Webhook redelivery is normal, not exceptional: a timeout on our side,
a retry on theirs, an at-least-once delivery guarantee. Reprocessing must not extend a pass
twice. The guarantee is the UNIQUE constraint on `purchases.tribute_purchase_id` — the
database refuses the second insert, and that refusal is the idempotency check rather than a
race-prone "select then insert".

**3. Answer 200 to a duplicate.** A duplicate is not an error; it is the system working.
Returning 4xx or 5xx makes Tribute retry forever and eventually alert on a webhook that is
in fact succeeding.

⚠️ THE PAYLOAD FIELD NAMES ARE UNCONFIRMED
------------------------------------------
`parse_event` is written from the plan's description of the webhook, not from a real
delivery, because the credentials are still outstanding (§4). The *structure* is right —
verify, resolve the buyer, apply once, log — and only the field names are a guess. So a
payload that does not parse is logged **in full** at ERROR level: the first real delivery
will tell you the true shape, and adapting is a change in one function.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Purchase, User
from api.services import events
from shared.config import settings
from shared.constants import (
    DEFAULT_LANG,
    EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED,
    TIER_DAYS,
    TIER_PRICE_CENTS,
    TIERS,
)

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "trbt-signature"

# Tribute's event names, per plan §4.1. The refund one is quoted there verbatim; the
# purchase one is inferred from the same naming, so both are matched loosely below.
PURCHASE_EVENTS = ("new_digital_product", "digital_product_purchased", "new_subscription")
REFUND_EVENTS = ("digital_product_refunded", "subscription_refunded")


class WebhookRejected(Exception):
    """Not processable. The route turns this into a 4xx, never a 5xx: a bad signature is
    not our failure to retry, it is a delivery we refuse."""


@dataclass(frozen=True)
class TributeEvent:
    kind: str            # "purchase" | "refund"
    purchase_id: str
    chat_id: int | None
    tier: str
    amount_cents: int
    currency: str


def verify(body: bytes, signature: str | None) -> None:
    """HMAC-SHA256 over the raw body, compared in constant time.

    Fails closed when no secret is configured. An unsigned webhook that grants a paid pass
    is a way to hand out the product for free to anyone who can guess the URL, so the
    absence of a secret must stop everything rather than skip the check.
    """
    secret = settings.tribute_webhook_secret
    if not secret:
        raise WebhookRejected(
            "TRIBUTE_WEBHOOK_SECRET is not configured — refusing to process webhooks"
        )
    if not signature:
        raise WebhookRejected(f"missing {SIGNATURE_HEADER} header")

    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    # Tribute's header may or may not be hex-lowercase; compare_digest is timing-safe and
    # the normalisation is cheap insurance against a spurious mismatch.
    if not hmac.compare_digest(expected, signature.strip().lower().removeprefix("sha256=")):
        raise WebhookRejected("signature does not match")


def _first(payload: dict, *names: str):
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def parse_event(body: bytes) -> TributeEvent:
    """Raw body -> what happened. See the warning in the module docstring."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise WebhookRejected(f"body is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookRejected("body is not a JSON object")

    # Tribute nests the interesting fields under `payload` in its documented examples.
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    merged = {**payload, **inner}

    name = str(_first(merged, "name", "event", "event_name", "type") or "").lower()
    if any(marker in name for marker in REFUND_EVENTS):
        kind = "refund"
    elif any(marker in name for marker in PURCHASE_EVENTS) or "purchase" in name:
        kind = "purchase"
    else:
        raise WebhookRejected(f"unrecognised event name {name!r}")

    purchase_id = _first(merged, "purchase_id", "id", "payment_id", "order_id")
    if purchase_id is None:
        raise WebhookRejected("no purchase id in payload")

    chat_id = _first(merged, "telegram_user_id", "telegram_id", "user_id", "chat_id")
    product = str(_first(merged, "product_id", "digital_product_id", "tier") or "")
    amount = _first(merged, "amount", "amount_cents", "price") or 0

    return TributeEvent(
        kind=kind,
        purchase_id=str(purchase_id),
        chat_id=int(chat_id) if chat_id is not None else None,
        tier=tier_for(product),
        amount_cents=int(amount),
        currency=str(_first(merged, "currency") or "EUR"),
    )


def tier_for(product: str) -> str:
    """Map Tribute's product id to one of our tiers.

    Falls back to the 1-month tier rather than refusing: a payment we cannot classify is
    still a payment, and granting the shorter pass errs towards the customer being
    under-served rather than towards us keeping money for nothing. It is logged.
    """
    if product and product == settings.tribute_product_3m:
        return TIERS[1] if len(TIERS) > 1 else TIERS[0]
    if product and product == settings.tribute_product_1m:
        return TIERS[0]
    if product in TIER_DAYS:
        return product
    log.warning("unrecognised Tribute product %r — defaulting to the shortest tier", product)
    return TIERS[0]


def extend(current: datetime | None, days: int, now: datetime) -> datetime:
    """Passes stack from whichever is later, so buying twice adds time.

    Shared with the admin grant: extending from `now` instead would silently shorten an
    active pass every time someone renewed early.
    """
    base = current if (current and current > now) else now
    return base + timedelta(days=days)


async def apply_purchase(session: AsyncSession, event: TributeEvent) -> str:
    """Extend the buyer's pass, exactly once. Returns what happened, for the response."""
    if event.chat_id is None:
        raise WebhookRejected("purchase has no telegram id — cannot credit anyone")

    now = datetime.now(timezone.utc)
    days = TIER_DAYS[event.tier]

    user = await session.get(User, event.chat_id)
    if user is None:
        # Paid before ever opening the bot. Losing the payment would be far worse than an
        # unexpected row, and /start is idempotent so it will adopt this one.
        user = User(chat_id=event.chat_id, lang=DEFAULT_LANG)
        session.add(user)
        await session.flush()
        log.info("created user %s from a purchase — they had not started the bot",
                 event.chat_id)

    expires = extend(user.pass_expires_at, days, now)
    purchase = Purchase(
        chat_id=event.chat_id,
        tribute_purchase_id=event.purchase_id,
        tier=event.tier,
        amount_cents=event.amount_cents or TIER_PRICE_CENTS.get(event.tier, 0),
        currency=event.currency,
        extended_to=expires,
    )
    session.add(purchase)
    try:
        # The UNIQUE constraint is the idempotency check. Doing this as "select, then
        # insert if absent" would leave a window for two concurrent redeliveries to both
        # pass the select.
        await session.flush()
    except IntegrityError:
        await session.rollback()
        log.info("purchase %s already applied — redelivery, ignoring", event.purchase_id)
        return "duplicate"

    user.pass_expires_at = expires
    await events.record(
        session,
        EV_PURCHASE_COMPLETED,
        chat_id=event.chat_id,
        purchase_id=event.purchase_id,
        tier=event.tier,
        amount_cents=purchase.amount_cents,
        currency=event.currency,
        expires_at=expires.isoformat(),
    )
    await session.commit()
    log.info("purchase %s: %s for %s, pass now to %s",
             event.purchase_id, event.tier, event.chat_id, expires)
    return "applied"


async def apply_refund(session: AsyncSession, event: TributeEvent) -> str:
    """Take back the time this purchase granted. Plan §4.1, and the EU withdrawal right.

    The days that *this* purchase added are subtracted, rather than the pass being cleared
    outright: someone who bought twice and had one refunded keeps what they still paid for.
    If that leaves the expiry in the past the pass is simply over, which is the immediate
    revocation a withdrawal requires.
    """
    purchase = await session.scalar(
        select(Purchase).where(Purchase.tribute_purchase_id == event.purchase_id)
    )
    if purchase is None:
        # Refund for something never recorded. Not an error to retry — there is nothing
        # here to revoke — but it means a purchase webhook was missed, which is worth
        # shouting about.
        log.error("refund for unknown purchase %s — a purchase webhook was probably "
                  "missed; check Tribute's delivery log", event.purchase_id)
        return "unknown"
    if purchase.refunded_at is not None:
        log.info("refund %s already applied — redelivery, ignoring", event.purchase_id)
        return "duplicate"

    now = datetime.now(timezone.utc)
    purchase.refunded_at = now

    user = await session.get(User, purchase.chat_id)
    if user is not None and user.pass_expires_at is not None:
        reduced = user.pass_expires_at - timedelta(days=TIER_DAYS[purchase.tier])
        user.pass_expires_at = reduced if reduced > now else now

    await events.record(
        session,
        EV_PURCHASE_REFUNDED,
        chat_id=purchase.chat_id,
        purchase_id=event.purchase_id,
        tier=purchase.tier,
        amount_cents=purchase.amount_cents,
        expires_at=user.pass_expires_at.isoformat() if user and user.pass_expires_at else None,
    )
    await session.commit()
    log.info("refund %s: %s revoked for %s", event.purchase_id, purchase.tier,
             purchase.chat_id)
    return "refunded"


async def handle(session: AsyncSession, body: bytes, signature: str | None) -> dict:
    """The whole of it: verify, parse, apply. Raises WebhookRejected for a 4xx."""
    verify(body, signature)
    try:
        event = parse_event(body)
    except WebhookRejected:
        # The first real delivery is the documentation. Logged whole, at ERROR, because
        # the field names here are written from the plan and not from Tribute.
        log.error("could not parse a Tribute webhook. Body follows so the real shape can "
                  "be read off it:\n%s", body.decode("utf-8", "replace")[:4000])
        raise

    if event.kind == "refund":
        return {"status": await apply_refund(session, event)}
    return {"status": await apply_purchase(session, event)}
