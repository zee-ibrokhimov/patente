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

FIELD NAMES ARE NOW READ FROM TRIBUTE'S OPENAPI SPEC
----------------------------------------------------
They were guessed from the plan until a real delivery could correct them. They are not
guessed any more — `telegram_user_id`, `subscription_id`, `amount`, `currency`,
`expires_at`, `type` and `period` all come from Tribute's published schema, and the
top-level `name` carries the event in snake_case.

A payload that still does not parse is logged **in full** at ERROR level, because the
alternative — a silent 400 — is how a working integration looks broken and a broken one
looks fine.

SUBSCRIPTIONS, NOT JUST ONE-OFF PURCHASES
-----------------------------------------
A subscription has a life beyond its first payment: it renews, and it gets cancelled.
Tribute owns that clock, so for subscription events the expiry comes from THEIR
`expires_at` rather than from our TIER_DAYS arithmetic — two systems computing the same
date independently will disagree eventually, and the one holding the card should win.

A cancellation does NOT revoke access. Cancelling means "do not bill me again", and the
period already paid for runs to its end. Revoking on cancel would take away time the
customer has bought, which is both wrong and a chargeback waiting to happen.
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
from api.services import events, notify
from shared.config import settings
from shared.constants import (
    DEFAULT_LANG,
    EV_PURCHASE_COMPLETED,
    EV_PURCHASE_REFUNDED,
    EV_SUBSCRIPTION_CANCELLED,
    EV_TRIAL_STARTED,
    TIER_DAYS,
    TIER_PRICE_CENTS,
    TIERS,
    TRIBUTE_PERIOD_TIERS,
    TRIBUTE_PERIODS_WITHOUT_TIER,
    TRIBUTE_TRIAL_FALLBACK_DAYS,
)

log = logging.getLogger(__name__)

SIGNATURE_HEADER = "trbt-signature"

# Tribute's event names, read off their OpenAPI spec rather than inferred. Their wiki
# lists them camelCase (`newSubscription`); the JSON body carries snake_case in `name`,
# which is what these match.
#
# `renewed_subscription` and `cancelled_subscription` were missing, and missing meant
# REJECTED — parse_event raises on an unrecognised name, so the route answered 400 and
# Tribute retried forever. A monthly subscriber's SECOND payment would have granted
# nothing: the kind of failure nobody sees until a customer says they lost access they
# had paid for.
PURCHASE_EVENTS = ("new_digital_product", "digital_product_purchased", "new_subscription")
RENEWAL_EVENTS = ("renewed_subscription",)
# Both spellings on purpose: Tribute writes this one with two Ls and its physical-order
# events with one, so picking either alone would be a coin flip.
CANCEL_EVENTS = ("cancelled_subscription", "canceled_subscription")
REFUND_EVENTS = ("digital_product_refunded", "subscription_refunded")

# `payload.type` on a subscription event: regular | gift | trial.
SUB_TRIAL = "trial"


class WebhookRejected(Exception):
    """Not processable. The route turns this into a 4xx, never a 5xx: a bad signature is
    not our failure to retry, it is a delivery we refuse."""


@dataclass(frozen=True)
class TributeEvent:
    kind: str            # "purchase" | "renewal" | "cancellation" | "refund"
    purchase_id: str
    chat_id: int | None
    tier: str
    amount_cents: int
    currency: str
    # Tribute's own end-of-period. Authoritative for subscriptions: they hold the card
    # and run the billing clock, and our TIER_DAYS arithmetic is only a fallback for
    # one-off digital products that carry no expiry.
    expires_at: datetime | None = None
    # A trial has a card attached and will convert, but no money has moved yet. It must
    # not read as a purchase — see apply_purchase.
    is_trial: bool = False


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

    # `type` is deliberately NOT in this list any more. On a subscription payload `type`
    # is regular/gift/trial, so falling back to it made the *subscription category* stand
    # in for the event name — which is how a trial could be read as an event called
    # "trial" and rejected.
    name = str(_first(merged, "name", "event", "event_name") or "").lower()

    # Order matters. `renewed_subscription` and `cancelled_subscription` must be tested
    # before the purchase markers, and the refund check must come first of all.
    if any(marker in name for marker in REFUND_EVENTS):
        kind = "refund"
    elif any(marker in name for marker in CANCEL_EVENTS):
        kind = "cancellation"
    elif any(marker in name for marker in RENEWAL_EVENTS):
        kind = "renewal"
    elif any(marker in name for marker in PURCHASE_EVENTS) or "purchase" in name:
        kind = "purchase"
    else:
        raise WebhookRejected(f"unrecognised event name {name!r}")

    purchase_id = _first(merged, "purchase_id", "id", "payment_id", "order_id",
                         "subscription_id")
    if purchase_id is None:
        raise WebhookRejected("no purchase id in payload")

    # A subscription sends the SAME subscription_id every renewal, so using it alone as
    # the idempotency key would make the second month look like a redelivery of the first
    # and grant nothing. Qualify it with the period it pays for.
    period_end = _parse_when(_first(merged, "expires_at", "expire_at", "period_end"))
    if kind in ("purchase", "renewal") and _first(merged, "purchase_id", "payment_id") is None:
        stamp = period_end.isoformat() if period_end else str(_first(merged, "created_at") or "")
        purchase_id = f"{purchase_id}:{stamp}"

    chat_id = _first(merged, "telegram_user_id", "telegram_id", "user_id", "chat_id")
    product = str(_first(merged, "product_id", "digital_product_id", "tier") or "")
    amount = _first(merged, "amount", "amount_cents", "price") or 0

    sub_type = str(_first(merged, "type") or "").lower()
    return TributeEvent(
        kind=kind,
        purchase_id=str(purchase_id),
        chat_id=int(chat_id) if chat_id is not None else None,
        tier=tier_for(product or str(_first(merged, "period") or "")),
        amount_cents=int(amount),
        currency=str(_first(merged, "currency") or "EUR"),
        expires_at=period_end,
        is_trial=sub_type == SUB_TRIAL,
    )


def _parse_when(value) -> datetime | None:
    """Tribute sends RFC3339 with a Z. Returns None rather than raising: a malformed
    date must not throw away a real payment, it must fall back to our own arithmetic."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        log.warning("could not parse a Tribute timestamp %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# The shortest tier by DAYS, not by position. TIERS is a tuple whose order is incidental,
# and "errs towards the customer being under-served" only holds if this is genuinely the
# smallest grant.
SHORTEST_TIER = min(TIER_DAYS, key=lambda t: TIER_DAYS[t])


def tier_for(product: str) -> str:
    """Map Tribute's product id to one of our tiers.

    Driven by settings.tribute_products rather than an if-chain per tier: the if-chain
    named 1m and 3m explicitly, so adding the 6-month tier meant a EUR 10.99 purchase
    fell through to "unrecognised" and granted 30 days — the customer pays for six months
    and gets one, with a log line as the only symptom.

    Still falls back rather than refusing: a payment we cannot classify is a payment, and
    granting the shortest pass errs towards under-serving the customer rather than
    keeping their money for nothing. It is logged loudly.
    """
    tier = settings.tribute_products.get(product)
    if tier:
        return tier
    # A subscription is identified by its billing period rather than by a product id.
    if product in TRIBUTE_PERIOD_TIERS:
        return TRIBUTE_PERIOD_TIERS[product]
    if product in TRIBUTE_PERIODS_WITHOUT_TIER:
        # Known, expected, and not a tier we sell. Quiet on purpose — see the constant.
        log.info("Tribute period %r has no tier of its own; the expiry comes from "
                 "their expires_at, so the label is cosmetic", product)
        return SHORTEST_TIER
    # Our own tier name, which is what the admin grant and the tests use.
    if product in TIER_DAYS:
        return product
    log.warning("unrecognised Tribute product %r — defaulting to the shortest tier", product)
    return SHORTEST_TIER


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

    if event.expires_at and event.expires_at > now:
        # A subscription: Tribute runs the billing clock and we follow it. `max` because
        # a subscriber who also bought a one-off pass must not have it truncated by a
        # renewal that happens to land earlier.
        current = user.pass_expires_at
        expires = max(event.expires_at, current) if current and current > now else event.expires_at
    elif event.is_trial:
        # A trial with an unreadable date. TIER_DAYS is wrong here — a trial has no tier,
        # so it resolves to the shortest PAID one and would hand out thirty days free on
        # the strength of a payload we could not parse. Logged at ERROR because an
        # unreadable date from a payment provider is worth someone looking at.
        log.error(
            "trial for %s arrived with no usable expires_at; granting %s days rather "
            "than the %s a tier fallback would have given",
            event.chat_id, TRIBUTE_TRIAL_FALLBACK_DAYS, days,
        )
        expires = extend(user.pass_expires_at, TRIBUTE_TRIAL_FALLBACK_DAYS, now)
    else:
        # A one-off digital product carries no expiry, so our own arithmetic applies.
        expires = extend(user.pass_expires_at, days, now)

    # A trial is stored at ZERO, and the usual `or TIER_PRICE_CENTS[...]` fallback is
    # skipped for it. `purchased` counts only rows with money against them, so writing
    # the list price here would make a trialling user read as a paying one and silence
    # the very pitch the trial exists to set up.
    amount = event.amount_cents
    if not event.is_trial:
        amount = amount or TIER_PRICE_CENTS.get(event.tier, 0)

    purchase = Purchase(
        extended_from=user.pass_expires_at,
        chat_id=event.chat_id,
        tribute_purchase_id=event.purchase_id,
        tier=event.tier,
        amount_cents=amount,
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
        EV_TRIAL_STARTED if event.is_trial else EV_PURCHASE_COMPLETED,
        chat_id=event.chat_id,
        purchase_id=event.purchase_id,
        tier=event.tier,
        amount_cents=purchase.amount_cents,
        currency=event.currency,
        expires_at=expires.isoformat(),
        renewal=event.kind == "renewal",
        trial=event.is_trial,
    )
    await session.commit()
    log.info("%s %s: %s for %s, pass now to %s",
             event.kind, event.purchase_id, event.tier, event.chat_id, expires)

    # AFTER the commit, and best-effort. The money is already recorded; a message that
    # fails to send must not turn a successful payment into a 4xx that Tribute retries.
    await notify.payment(
        event.chat_id, user.lang,
        "trial" if event.is_trial else "paid",
        expires, event.tier,
    )
    return "trial" if event.is_trial else ("renewed" if event.kind == "renewal" else "applied")


async def apply_refund(session: AsyncSession, event: TributeEvent) -> str:
    """Take back the time this purchase granted. Plan §4.1, and the EU withdrawal right.

    The days that *this* purchase added are subtracted, rather than the pass being cleared
    outright: someone who bought twice and had one refunded keeps what they still paid for.
    If that leaves the expiry in the past the pass is simply over, which is the immediate
    revocation a withdrawal requires.
    """
    # Exact match first, then a prefix match on the subscription id.
    #
    # A subscription's purchase rows are keyed "<subscription_id>:<period_end>" so that
    # month two is not mistaken for a redelivery of month one — every renewal carries the
    # SAME subscription_id. But a refund payload has no period, so it arrives as the bare
    # id and could never match the row it refers to. The result was silent: apply_refund
    # returned "unknown purchase", the pass stood, and /admin still counted the money as
    # revenue because it filters on refunded_at IS NULL.
    #
    # Newest un-refunded first, because a refund names a subscription rather than a
    # particular month, and taking back the most recent period is what Tribute means.
    purchase = await session.scalar(
        select(Purchase).where(Purchase.tribute_purchase_id == event.purchase_id)
    )
    if purchase is None:
        purchase = await session.scalar(
            select(Purchase)
            .where(
                Purchase.chat_id == event.chat_id,
                Purchase.tribute_purchase_id.like(f"{event.purchase_id}:%"),
                Purchase.refunded_at.is_(None),
            )
            .order_by(Purchase.id.desc())
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
        # Subtract the time THIS purchase granted, rather than restoring the expiry it
        # replaced. The difference matters when there are several: restoring an absolute
        # previous value would, on refunding the FIRST of two purchases, throw away the
        # second one's time as well — which an older test caught, correctly.
        #
        # It used to subtract TIER_DAYS[tier], our own idea of how long a tier lasts. A
        # subscription grants TRIBUTE's expires_at, a real billing period that is not
        # exactly 30 or 90 days, so the two disagreed: revoking a 31-day month took 30 and
        # left a free day. `extended_from` and `extended_to` bracket what was actually
        # given, so the subtraction is now exact.
        granted = None
        if purchase.extended_to is not None:
            base = purchase.extended_from or purchase.created_at
            if base is not None:
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
                to = purchase.extended_to
                if to.tzinfo is None:
                    to = to.replace(tzinfo=timezone.utc)
                granted = max(timedelta(0), to - base)

        if granted is None:
            # A row from before extended_from existed. Approximate, but better than
            # refusing to revoke at all.
            granted = timedelta(days=TIER_DAYS[purchase.tier])

        reduced = user.pass_expires_at - granted
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
    # Premium has three sources and a refund only ever revoked ONE of them.
    #
    # Tribute sells a subscription attached to a channel and adds buyers to it, and
    # membership grants Premium in its own right — deliberately, so a webhook we never
    # received cannot lock out someone who paid. The reverse case was never handled: a
    # refunded buyer whose pass was revoked stayed a channel member, so `via_channel`
    # kept them Premium indefinitely. Their money back AND the product.
    #
    # Tribute should remove them from the channel itself; this forces the check now
    # rather than waiting up to fifteen minutes for the cache to expire. If they are
    # STILL a member afterwards, that is Tribute's removal not having happened and it is
    # logged loudly, because no amount of code here can eject someone from a channel we
    # do not own.
    from api.services import channel as channel_service

    status = await channel_service.refresh(session, user, force=True)
    if channel_service.grants_premium(status):
        log.error(
            "refund %s: %s is STILL in the Premium channel (%s), so channel membership "
            "keeps granting them Premium. Remove them in Tribute.",
            event.purchase_id, event.chat_id, status,
        )

    log.info("refund %s: %s revoked for %s", event.purchase_id, purchase.tier,
             purchase.chat_id)
    return "refunded"


async def apply_cancellation(session: AsyncSession, event: TributeEvent) -> str:
    """Record that a subscription will not renew. Deliberately does NOT revoke access.

    Cancelling means "do not bill me again", not "refund me and cut me off now". The
    period already paid for runs to its end — taking it back would be removing time the
    customer has bought, and is how a cancellation turns into a chargeback.

    So the pass is left exactly as it is. `pass_expires_at` was already set by the
    purchase or the most recent renewal, and with nothing further to extend it, it simply
    lapses on its own. The event is written because otherwise churn is invisible: the
    only trace of a cancellation would be a renewal that never arrived.
    """
    if event.chat_id is None:
        raise WebhookRejected("cancellation has no telegram id")

    user = await session.get(User, event.chat_id)
    if user is None:
        # Nothing to record against, and nothing to take away. Not an error: answering
        # 4xx would make Tribute retry a delivery that can never succeed.
        log.warning("cancellation for unknown user %s — ignoring", event.chat_id)
        return "unknown-user"

    await events.record(
        session,
        EV_SUBSCRIPTION_CANCELLED,
        chat_id=event.chat_id,
        purchase_id=event.purchase_id,
        tier=event.tier,
        trial=event.is_trial,
        # When their access actually stops, which is what support will be asked.
        access_until=(user.pass_expires_at.isoformat() if user.pass_expires_at else None),
    )
    await session.commit()
    log.info("subscription cancelled for %s (trial=%s); access stands until %s",
             event.chat_id, event.is_trial, user.pass_expires_at)

    # Worth sending precisely because the answer is reassuring: they keep what they paid
    # for. Silence here reads as "cancelled, access gone", which is the opposite of true.
    await notify.payment(event.chat_id, user.lang, "cancelled",
                         user.pass_expires_at, event.tier)
    return "cancelled"


# A body is a few hundred bytes. Capped anyway: an unbounded column is how one malformed
# delivery fills a disk, which is not a hypothetical failure mode on this machine.
MAX_BODY = 8000


async def record_delivery(
    session: AsyncSession, body: bytes, *, ok: bool, outcome: str
) -> None:
    """Keep what Tribute sent, verbatim, whatever happened to it.

    The only forensic record was a container's stdout. Today a redeploy replaced the
    container and took six hours of webhook history with it — during an outage, which is
    exactly when it was worth having. When a customer says "I paid and got nothing", the
    question is what Tribute actually sent, and the answer needs to survive a deploy.

    The RAW bytes are stored rather than a parsed copy: the HMAC covers exact bytes, so a
    re-serialised version could never re-verify a disputed delivery, and the fields we did
    not understand are the ones worth reading later.

    Never raises. A delivery must not be rejected because the audit row could not be
    written — the money side has already been decided by the time this runs.
    """
    from api.models import WebhookDelivery

    text = body.decode("utf-8", "replace")[:MAX_BODY]
    name = chat_id = None
    try:
        payload = json.loads(text)
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        merged = {**payload, **inner}
        name = str(_first(merged, "name", "event", "event_name") or "") or None
        raw_id = _first(merged, "telegram_user_id", "telegram_id", "user_id", "chat_id")
        chat_id = int(raw_id) if raw_id is not None else None
    except Exception:  # noqa: BLE001 — an unparseable body is exactly what this is for
        pass

    try:
        session.add(WebhookDelivery(
            name=name, chat_id=chat_id, outcome=outcome[:200],
            signature_ok=ok, body=text,
        ))
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not record a webhook delivery: %s", exc)


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
    if event.kind == "cancellation":
        return {"status": await apply_cancellation(session, event)}
    # A renewal is a purchase with a new period: same money, same idempotency, same
    # extension. Only the analytics event and the log line differ.
    return {"status": await apply_purchase(session, event)}
