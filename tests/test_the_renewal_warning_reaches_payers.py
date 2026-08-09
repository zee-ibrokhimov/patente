"""The people who pay are the people who get warned their access is ending.

They were the only ones who did not.

`_has_subscription` asked "has money ever moved for this user", and skipped anyone it
answered yes for. That was sound while Tribute existed: a subscription renews itself, so
warning someone it is about to happen is noise.

Payments are direct now, and the grant route writes a Purchase row for every hand sale. So
a paying customer became indistinguishable from a Tribute subscriber and was skipped —
while gift recipients, who paid nothing and had no row, were warned. The renewal ask is the
conversation this business runs on, and it was reaching exactly the wrong half of the list.

A manual sale is identified by MANUAL_PURCHASE_PREFIX in `tribute_purchase_id`, which is
why that prefix is a shared constant: the grant route writes it and this reads it, and if
the two ever disagree the bug comes straight back with no test noticing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.models import Purchase, User
from api.services import lapse, notify
from shared.constants import MANUAL_PURCHASE_PREFIX


def in_days(n: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=n)


async def sent_by_lapse(api_db) -> list[tuple[int, str]]:
    """Run the notifier with the network stubbed; return (chat_id, kind) sent."""
    out: list[tuple[int, str]] = []

    async def fake(chat_id, lang, kind, expires_at, tier, days=0):
        out.append((chat_id, kind))
        return True

    original = notify.payment
    notify.payment = fake
    try:
        async with api_db() as s:
            await lapse.run(s)
    finally:
        notify.payment = original
    return out


async def a_user(api_db, chat_id: int, *, expires_in: float, purchase_id: str | None):
    async with api_db() as s:
        s.add(User(chat_id=chat_id, lang="ru", pass_expires_at=in_days(expires_in)))
        if purchase_id:
            s.add(Purchase(chat_id=chat_id, tribute_purchase_id=purchase_id,
                           tier="month", amount_cents=999, currency="EUR"))
        await s.commit()


# --- the bug ----------------------------------------------------------------

async def test_a_hand_sold_customer_is_warned(api_db):
    """THE regression. They paid, so there is a Purchase row, so the old check called them
    a subscriber and stayed silent — for the one person most worth keeping."""
    await a_user(api_db, 9301, expires_in=2,
                 purchase_id=f"{MANUAL_PURCHASE_PREFIX}9301:20260809120000:abc123")
    sent = await sent_by_lapse(api_db)
    assert (9301, "ending") in sent, f"a paying customer was not warned: {sent}"


async def test_a_gift_recipient_is_still_warned(api_db):
    """They were the only ones being warned before, and they should still be."""
    await a_user(api_db, 9302, expires_in=2, purchase_id=None)
    assert (9302, "ending") in await sent_by_lapse(api_db)


async def test_a_real_tribute_subscriber_is_still_skipped(api_db):
    """Guards the guard. Warning everybody would pass the first test and reintroduce the
    noise the check was written to prevent — telling someone a subscription that renews
    itself is about to renew itself."""
    await a_user(api_db, 9303, expires_in=2, purchase_id="tribute-abc-123")
    sent = await sent_by_lapse(api_db)
    assert (9303, "ending") not in sent, \
        f"a genuine auto-renewing subscriber was warned anyway: {sent}"


async def test_a_refunded_sale_does_not_suppress_the_warning(api_db):
    """A refunded purchase is not a subscription about to renew — it is money already given
    back. Treating it as one silences the warning for somebody whose access really is about
    to stop."""
    async with api_db() as s:
        s.add(User(chat_id=9304, lang="ru", pass_expires_at=in_days(2)))
        s.add(Purchase(chat_id=9304, tribute_purchase_id="tribute-refunded-1",
                       tier="month", amount_cents=999, currency="EUR",
                       refunded_at=datetime.now(timezone.utc)))
        await s.commit()
    assert (9304, "ending") in await sent_by_lapse(api_db)


# --- the prefix is shared, not repeated -------------------------------------

def test_the_writer_and_the_reader_use_the_same_prefix():
    """If the grant route's literal and the check's literal ever drift apart, every hand
    sale silently becomes a "subscription" again and the warnings stop — with nothing
    failing."""
    grant = open("api/routes/webapp_admin.py", encoding="utf-8").read()
    check = open("api/services/lapse.py", encoding="utf-8").read()
    assert "MANUAL_PURCHASE_PREFIX" in grant, "the grant route hardcodes the prefix again"
    assert "MANUAL_PREFIX" in check or "MANUAL_PURCHASE_PREFIX" in check, \
        "the lapse check hardcodes the prefix again"
