"""Somebody who has just paid must not be told they are on a free trial.

Found in the production event log rather than by reading code. Payment moved to hand-grants
on 2026-08-09 and nothing wrote a Purchase row, so:

  · `plan()` branches on `purchased`, which IS a Purchase row. Without one, a paying
    customer opening /plan after paying EUR 10.99 was told "Free trial — 30 days left.
    Nothing will be charged."
  · every revenue figure counted `amount_cents > 0` over a table nothing was writing to,
    so the number was zero and would have stayed zero for ever.

Both from the same omission, and both invisible until somebody actually paid.

A grant with amount 0 still produces the trial wording, and that is correct: a comp, a
tester and an apology are not sales, and calling them one would corrupt the same figures in
the other direction.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Purchase, User
from api.routes import users as users_route
from api.services.telegram_auth import sign
from bot import render
from shared.config import settings

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42


@pytest.fixture(autouse=True)
def _staff(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "admin_chat_ids", str(OWNER))


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def grant(client, chat_id: int, *, days=30, cents=0):
    return await client.post(f"/webapp/admin/users/{chat_id}/grant", headers=auth(),
                             json={"days": days, "amount_cents": cents,
                                   "reason": "test"})


# --- the message they read ---------------------------------------------------

async def test_a_paying_customer_is_told_their_subscription_is_active(
        client, registered, api_db):
    """THE bug. They paid, and the app said "free trial, nothing will be charged"."""
    r = await grant(client, OWNER, days=30, cents=1099)
    assert r.status_code == 200, r.text

    async with api_db() as s:
        user = await s.get(User, OWNER)
        payload = await users_route._out(user, s)

    text = render.plan(payload.model_dump(), "en", can_subscribe=False)
    assert "trial" not in text.lower(), f"a paying customer was called a trialist:\n{text}"
    assert "active" in text.lower()


async def test_a_free_comp_is_still_called_a_trial(client, registered, api_db):
    """The other direction. A comp, a tester and an apology are not sales, and recording
    them as one would corrupt the same numbers the other way."""
    r = await grant(client, OWNER, days=30, cents=0)
    assert r.status_code == 200

    async with api_db() as s:
        user = await s.get(User, OWNER)
        payload = await users_route._out(user, s)
    assert payload.purchased is False


# --- the money ---------------------------------------------------------------

async def test_a_sale_is_recorded(client, registered, api_db):
    await grant(client, OWNER, days=90, cents=799)
    async with api_db() as s:
        rows = (await s.scalars(select(Purchase).where(Purchase.chat_id == OWNER))).all()
    assert len(rows) == 1
    assert rows[0].amount_cents == 799
    assert rows[0].currency == "EUR"


async def test_a_free_grant_records_no_sale(client, registered, api_db):
    await grant(client, OWNER, days=30, cents=0)
    async with api_db() as s:
        rows = (await s.scalars(select(Purchase).where(Purchase.chat_id == OWNER))).all()
    assert rows == []


async def test_revenue_is_reported(client, registered):
    await grant(client, OWNER, days=30, cents=1099)
    await grant(client, OWNER, days=30, cents=299)
    body = (await client.get("/webapp/admin/overview", headers=auth())).json()
    assert body["revenue_cents"] == 1398
    assert body["paid_purchases"] == 2


async def test_the_purchase_records_what_the_pass_was_extended_to(client, registered, api_db):
    """`extended_from`/`extended_to` are what make a refund exact. A manual sale that does
    not record them would be a sale that cannot be reversed accurately."""
    await grant(client, OWNER, days=30, cents=1099)
    async with api_db() as s:
        row = (await s.scalars(select(Purchase))).one()
        user = await s.get(User, OWNER)
    assert row.extended_to == user.pass_expires_at


async def test_two_sales_to_one_person_do_not_collide(client, registered, api_db):
    """`tribute_purchase_id` is UNIQUE and NOT NULL, and there is no Tribute any more — the
    synthetic id has to stay unique or the second sale to the same person fails."""
    await grant(client, OWNER, days=30, cents=299)
    await grant(client, OWNER, days=30, cents=299)
    async with api_db() as s:
        rows = (await s.scalars(select(Purchase))).all()
    assert len({r.tribute_purchase_id for r in rows}) == len(rows) == 2


async def test_an_absurd_amount_is_refused(client, registered):
    r = await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(),
                          json={"days": 30, "amount_cents": 999_999_999})
    assert r.status_code == 422


async def test_the_amount_is_on_the_event_too(client, registered, api_db):
    """The Purchase row is the ledger; the event is the audit trail of who did it and why.
    Both matter when reconciling a month of hand-sold access."""
    from api.models import Event
    from shared.constants import EV_PASS_GRANTED

    await grant(client, OWNER, days=30, cents=1099)
    async with api_db() as s:
        row = (await s.scalars(select(Event).where(
            Event.type == EV_PASS_GRANTED))).all()[-1]
    assert row.payload["amount_cents"] == 1099
    assert row.payload["by"] == OWNER
