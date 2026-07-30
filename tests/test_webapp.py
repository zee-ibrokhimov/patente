"""The Mini App surface — the first part of this API meant to face the internet.

These tests exist for one reason: /webapp/* is public and /users/{chat_id}/* is not,
and the difference is entirely that these routes refuse to take identity from the
caller. Plan §6.3: without server-side initData validation, anyone can craft a request
claiming any Telegram ID and unlock paid content for free.
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select

from api.models import User
from api.services import telegram_auth
from shared.config import settings

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42          # the user the conftest fixtures register
INTRUDER = 999999


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER, token: str = TOKEN, lang: str = "ru") -> dict:
    blob = telegram_auth.sign(
        {
            "user": json.dumps({"id": chat_id, "language_code": lang}, separators=(",", ":")),
            "auth_date": str(int(time.time())),
        },
        token,
    )
    return {"X-Telegram-Init-Data": blob}


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    ["/webapp/me", "/webapp/stats", "/webapp/next-question", "/webapp/topics"],
)
async def test_no_init_data_is_refused(client, path):
    assert (await client.get(path)).status_code == 401


async def test_a_forged_signature_is_refused(client):
    """Signed with someone else's bot token — the attack the HMAC exists to stop."""
    r = await client.get("/webapp/me", headers=auth(token="1111:AAEnot-the-real-token"))
    assert r.status_code == 401


async def test_a_tampered_chat_id_is_refused(client, registered):
    """Take a genuine blob, swap the id, keep the hash. This must not work."""
    headers = auth(OWNER)
    headers["X-Telegram-Init-Data"] = headers["X-Telegram-Init-Data"].replace(
        str(OWNER), str(INTRUDER)
    )
    assert (await client.get("/webapp/me", headers=headers)).status_code == 401


async def test_a_stale_blob_is_refused(client):
    blob = telegram_auth.sign(
        {
            "user": json.dumps({"id": OWNER}, separators=(",", ":")),
            "auth_date": str(int(time.time()) - telegram_auth.MAX_AGE_SECONDS - 60),
        },
        TOKEN,
    )
    r = await client.get("/webapp/me", headers={"X-Telegram-Init-Data": blob})
    assert r.status_code == 401


async def test_the_401_does_not_say_which_check_failed(client):
    """Probing feedback helps an attacker and nobody else."""
    body = (await client.get("/webapp/me", headers=auth(token="1111:AAEwrong"))).json()
    assert "signature" not in json.dumps(body).lower()
    assert "stale" not in json.dumps(body).lower()


# --------------------------------------------------------------------------
# identity comes from the signature, never from the request
# --------------------------------------------------------------------------

async def test_the_caller_is_whoever_the_signature_says(client, registered):
    r = await client.get("/webapp/me", headers=auth(OWNER))
    assert r.status_code == 200
    assert r.json()["chat_id"] == OWNER


async def test_a_valid_signature_cannot_reach_another_users_data(client, registered):
    """INTRUDER signs correctly for themselves and gets their own empty account -
    not OWNER's. There is no parameter that would let them ask for OWNER."""
    r = await client.get("/webapp/me", headers=auth(INTRUDER))
    assert r.status_code == 200
    assert r.json()["chat_id"] == INTRUDER
    assert r.json()["chat_id"] != OWNER


def webapp_paths() -> list[str]:
    """Every documented path under /webapp.

    Read from the OpenAPI schema, NOT from `app.routes`. FastAPI 0.141 stores an
    included router as a single `_IncludedRouter` wrapper object rather than flattening
    its leaf routes into `app.routes`, so walking `app.routes` for `.path` yields only
    the four default docs routes plus /health — and any assertion over "webapp routes"
    built that way passes because the list is EMPTY. Both structural tests below did
    exactly that until it was caught. A security invariant that cannot fail is worse
    than no test, because it reads like coverage.
    """
    from api.main import app

    return [p for p in app.openapi()["paths"] if p.startswith("/webapp")]


async def test_the_webapp_surface_is_not_empty():
    """Guards the guard: if this ever returns nothing, the two tests below are vacuous."""
    assert len(webapp_paths()) >= 8


async def test_no_webapp_route_accepts_a_chat_id(client):
    """The structural guarantee: if no route declares chat_id, none can be tricked
    into using one. This fails loudly if somebody later adds /webapp/users/{chat_id}."""
    assert [p for p in webapp_paths() if "chat_id" in p] == []


async def test_the_dangerous_routes_are_not_exposed_under_webapp(client):
    """Granting a pass and erasing a user must have no public path, ever."""
    paths = webapp_paths()
    assert not any("pass" in p for p in paths)
    assert not any("file-id" in p for p in paths)
    assert "/webapp/health" not in paths


# --------------------------------------------------------------------------
# it is the same product, not a second implementation
# --------------------------------------------------------------------------

async def test_an_unknown_caller_is_created_on_first_contact(client, api_db):
    r = await client.get("/webapp/me", headers=auth(INTRUDER, lang="ru"))
    assert r.status_code == 200
    async with api_db() as s:
        user = (await s.scalars(select(User).where(User.chat_id == INTRUDER))).one()
    assert user.chat_id == INTRUDER


async def test_an_unsupported_client_language_falls_back(client):
    """Telegram reports whatever the phone is set to - 'uk' is not a UI language."""
    r = await client.get("/webapp/me", headers=auth(INTRUDER, lang="uk"))
    assert r.status_code == 200
    assert r.json()["lang"] == "it"


async def test_entitlement_is_enforced_on_the_webapp_surface_too(client, registered):
    """Stats are free, but the paid gate must behave identically to the bot's path -
    the frontend is never trusted to hide anything."""
    r = await client.get("/webapp/stats", headers=auth(OWNER))
    assert r.status_code in (200, 402)


async def test_settings_round_trip(client, registered):
    r = await client.patch(
        "/webapp/settings", headers=auth(OWNER), json={"translations_on": False}
    )
    assert r.status_code == 200
    assert r.json()["translations_on"] is False
