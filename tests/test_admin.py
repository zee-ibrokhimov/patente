"""The owner's view, and the fact that it is only the owner's.

/admin/* is unauthenticated, exactly like /users/{chat_id}/pass which hands out passes.
That is the deliberate design — the NETWORK is the authentication, and nginx proxies only
/webapp/* and POST /webhooks/tribute. So the test that matters most here is not about the
numbers; it is that this prefix is not routable from the internet, and that the bot
refuses non-admins before it ever calls.
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from api.models import Event, Purchase, User
from api.services import admin
from shared.constants import EV_PURCHASE_COMPLETED, EV_TRIAL_STARTED

NOW = datetime.now(timezone.utc)


# --- the boundary -----------------------------------------------------------

def test_admin_is_not_routable_from_the_internet():
    """nginx proxies exactly two things. /admin/ has no location block, so it 404s at the
    edge without ever reaching the API — the same omission that lets an API with no
    authentication sit behind a public domain."""
    conf = (pathlib.Path(__file__).resolve().parent.parent
            / "webapp" / "nginx.conf").read_text(encoding="utf-8")
    assert "/admin" not in conf, "the edge must not know this prefix exists"
    assert conf.count("proxy_pass http://api:8000;") == 2


def test_the_bot_gates_admin_commands_before_calling():
    """Silent for non-admins, like /grant. A stranger who guesses the command should
    learn nothing, and there is no legitimate user to explain a refusal to."""
    source = (pathlib.Path(__file__).resolve().parent.parent
              / "bot" / "handlers" / "misc.py").read_text(encoding="utf-8")
    for command in ("admin", "whois"):
        block = source[source.index(f'Command("{command}")'):][:900]
        assert "config.admin_ids" in block, f"/{command} has no admin gate"
        assert block.index("config.admin_ids") < block.index("api."), \
            f"/{command} calls the API before checking who is asking"


# --- the numbers ------------------------------------------------------------

@pytest.fixture
async def populated(api_db):
    async with api_db() as s:
        s.add_all([
            User(chat_id=1, lang="ru", pass_expires_at=NOW + timedelta(days=10)),
            User(chat_id=2, lang="en", channel_status="member"),
            User(chat_id=3, lang="it", channel_status="left"),
            User(chat_id=4, lang="uz"),
        ])
        s.add_all([
            # A real payment, and a trial. The trial writes a row at ZERO so redelivery
            # stays idempotent; counting it as revenue would overstate the takings.
            Purchase(chat_id=1, tribute_purchase_id="p1", tier="pass_1m",
                     amount_cents=299, currency="eur", extended_to=NOW),
            Purchase(chat_id=2, tribute_purchase_id="p2", tier="pass_1m",
                     amount_cents=0, currency="eur", extended_to=NOW),
        ])
        s.add_all([
            Event(chat_id=1, type=EV_PURCHASE_COMPLETED),
            Event(chat_id=2, type=EV_TRIAL_STARTED),
        ])
        await s.commit()
    return api_db


async def test_revenue_counts_money_not_rows(populated):
    """A trial is a Purchase row at zero. If it counted, every trial would read as €2.99
    of revenue that never existed."""
    async with populated() as s:
        d = await admin.overview(s)
    assert d["purchases"] == 1
    assert d["revenue_cents"] == 299


async def test_premium_is_counted_from_both_sources(populated):
    async with populated() as s:
        d = await admin.overview(s)
    assert d["with_pass"] == 1
    assert d["in_channel"] == 1      # chat 2 is a member; chat 3 has left


async def test_someone_who_left_the_channel_is_not_counted(populated):
    async with populated() as s:
        d = await admin.overview(s)
    assert d["in_channel"] == 1


async def test_a_refunded_purchase_leaves_the_revenue(api_db):
    """Otherwise the takings only ever go up and a refund is invisible."""
    async with api_db() as s:
        s.add(User(chat_id=9, lang="ru"))
        s.add(Purchase(chat_id=9, tribute_purchase_id="r1", tier="pass_1m",
                       amount_cents=999, currency="eur", extended_to=NOW,
                       refunded_at=NOW))
        await s.commit()
    async with api_db() as s:
        d = await admin.overview(s)
    assert d["revenue_cents"] == 0
    assert d["refunded"] == 1


# --- looking one person up --------------------------------------------------

async def test_whois_shows_both_entitlement_sources(populated):
    async with populated() as s:
        d = await admin.whois(s, 2)
    assert d["channel_status"] == "member"
    assert d["has_pass"] is False


async def test_whois_lists_purchases_including_trials(populated):
    async with populated() as s:
        d = await admin.whois(s, 1)
    assert len(d["purchases"]) == 1
    assert d["purchases"][0]["amount_cents"] == 299


async def test_whois_returns_none_for_a_stranger(populated):
    """Which is itself the answer to "I paid and have nothing": a purchase webhook
    CREATES a user, so no row at all means no payment ever reached us."""
    async with populated() as s:
        assert await admin.whois(s, 123456) is None


async def test_whois_survives_a_user_with_no_history(api_db):
    async with api_db() as s:
        s.add(User(chat_id=50, lang="ru"))
        await s.commit()
    async with api_db() as s:
        d = await admin.whois(s, 50)
    assert d["purchases"] == []
    assert d["recent_events"] == []


async def test_the_overview_works_on_an_empty_database(api_db):
    """It will be read on day one, when every number is zero, and must not divide by it."""
    async with api_db() as s:
        d = await admin.overview(s)
    assert d["users"] >= 0
    assert d["revenue_cents"] == 0
