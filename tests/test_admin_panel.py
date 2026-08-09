"""The owner's console: who can reach it, and what it refuses to do.

This surface can give away paid access, message every user, and read personal data. It is
the most dangerous thing in the product, and the API it is bolted to has NO authentication
of its own — the founding rule is that /users/{chat_id}/… must never leave loopback because
the caller simply asserts who they are.

It is safe only because it lives inside the Mini App, behind the Telegram-signed `initData`
whose HMAC is checked before anything else runs, and behind a staff dependency on every
route. The tests that matter most here are therefore not the features. They are:

  · a non-staff caller can reach NOTHING, and is told 404 rather than 403 — a 403 confirms
    the endpoint exists and that the caller merely lacks the rank;
  · an unsigned caller can reach nothing;
  · every route in the file carries the dependency, checked by reflection so a route added
    later without it fails this suite rather than shipping.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, ReferralLink, User
from api.routes import webapp_admin
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_PASS_GRANTED

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
STRANGER = 4242


@pytest.fixture(autouse=True)
def _staff(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")
    # OWNER is staff; nobody else is.
    monkeypatch.setattr(settings, "admin_chat_ids", str(OWNER))


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


ROUTES = [
    ("GET", "/webapp/admin/overview", None),
    ("GET", "/webapp/admin/users", None),
    ("GET", "/webapp/admin/links", None),
    ("GET", "/webapp/admin/broadcast/history", None),
    ("POST", "/webapp/admin/users/42/grant", {"days": 30}),
    ("POST", "/webapp/admin/links", {"code": "abc", "trial_days": 7}),
    ("PATCH", "/webapp/admin/links/abc", {"active": False}),
    ("POST", "/webapp/admin/message", {"chat_id": 42, "text": "hi"}),
    ("POST", "/webapp/admin/broadcast/preview", {"text": "hi"}),
    ("POST", "/webapp/admin/broadcast", {"text": "hi", "confirm_recipients": 1}),
]


# --- the boundary ------------------------------------------------------------

@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_a_stranger_reaches_nothing(client, registered, method, path, body):
    r = await client.request(method, path, headers=auth(STRANGER), json=body)
    assert r.status_code == 404, f"{method} {path} answered {r.status_code} to a stranger"


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_an_unsigned_caller_reaches_nothing(client, registered, method, path, body):
    r = await client.request(method, path, json=body)
    assert r.status_code == 401


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_a_forged_signature_reaches_nothing(client, registered, method, path, body):
    forged = {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": OWNER}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, "9999:not-the-real-token")}
    r = await client.request(method, path, headers=forged, json=body)
    assert r.status_code == 401


async def test_it_answers_404_rather_than_403(api_db):
    """A 403 confirms the endpoint is there and the caller merely lacks the rank. To
    anything probing this surface, present-but-forbidden is an invitation.

    Asserted on the raised status rather than on the source. The first version grepped the
    function text and matched the word "403" in its own docstring — the same way a test
    earlier in this project failed on its own explanation.
    """
    from fastapi import HTTPException

    async with api_db() as s:
        stranger = User(chat_id=STRANGER, lang="ru")
        s.add(stranger)
        await s.commit()

    with pytest.raises(HTTPException) as raised:
        await webapp_admin.staff_user(stranger)
    assert raised.value.status_code == 404


def test_every_route_carries_the_staff_dependency():
    """The whole authorisation model is one dependency. A route added later without it is a
    route anyone can call, and reviewing that by eye is exactly what gets missed."""
    import inspect

    unguarded = []
    for route in webapp_admin.router.routes:
        params = inspect.signature(route.endpoint).parameters
        guarded = any(
            getattr(p.default, "dependency", None) is webapp_admin.staff_user
            for p in params.values()
        )
        if not guarded:
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert unguarded == [], f"routes with no staff check: {unguarded}"


# --- granting access ---------------------------------------------------------

async def test_the_owner_can_grant_access(client, registered, api_db):
    r = await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(),
                          json={"days": 30, "reason": "paid by bank transfer"})
    assert r.status_code == 200, r.text
    async with api_db() as s:
        user = await s.get(User, OWNER)
    assert user.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=29)


async def test_a_grant_extends_rather_than_replaces(client, registered, api_db):
    """Somebody paying for a second month while the first is still running must not lose
    the remainder. Granting from `now` would silently shorten what they already had."""
    async with api_db() as s:
        user = await s.get(User, OWNER)
        user.pass_expires_at = datetime.now(timezone.utc) + timedelta(days=20)
        await s.commit()

    await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(),
                      json={"days": 30})
    async with api_db() as s:
        user = await s.get(User, OWNER)
    assert user.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=49)


async def test_an_expired_pass_grants_from_today(client, registered, api_db):
    """The other half: extending from a date in the past would grant nothing at all."""
    async with api_db() as s:
        user = await s.get(User, OWNER)
        user.pass_expires_at = datetime.now(timezone.utc) - timedelta(days=100)
        await s.commit()

    await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(), json={"days": 7})
    async with api_db() as s:
        user = await s.get(User, OWNER)
    assert user.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=6)


async def test_a_grant_is_recorded_with_who_did_it(client, registered, api_db):
    await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(),
                      json={"days": 30, "reason": "revolut"})
    async with api_db() as s:
        row = (await s.scalars(select(Event).where(
            Event.type == EV_PASS_GRANTED))).all()[-1]
    assert row.payload["by"] == OWNER
    assert row.payload["reason"] == "revolut"


async def test_a_slipped_zero_cannot_grant_ten_years(client, registered):
    r = await client.post(f"/webapp/admin/users/{OWNER}/grant", headers=auth(),
                          json={"days": 36500})
    assert r.status_code == 422


async def test_granting_to_a_stranger_is_refused(client, registered):
    r = await client.post("/webapp/admin/users/999999/grant", headers=auth(),
                          json={"days": 30})
    assert r.status_code == 404


# --- referral links ----------------------------------------------------------

async def test_a_link_can_be_created_and_carries_its_url(client, registered, monkeypatch):
    monkeypatch.setattr(settings, "bot_username", "quizpatente_bot")
    r = await client.post("/webapp/admin/links", headers=auth(),
                          json={"code": "tg_uzbeks", "label": "Uzbek channel",
                                "trial_days": 14, "max_uses": 100})
    assert r.status_code == 201, r.text

    body = (await client.get("/webapp/admin/links", headers=auth())).json()
    link = body["links"][0]
    assert link["code"] == "tg_uzbeks"
    assert link["trial_days"] == 14
    assert link["url"] == "https://t.me/quizpatente_bot?start=tg_uzbeks"
    assert link["uses"] == 0


async def test_a_code_is_cleaned_the_same_way_the_start_payload_is(client, registered):
    """A code that survives here but not in `_clean_source` would be a link that cannot
    grant what it promises."""
    r = await client.post("/webapp/admin/links", headers=auth(),
                          json={"code": "tg uzbeks!!", "trial_days": 7})
    assert r.status_code == 201
    body = (await client.get("/webapp/admin/links", headers=auth())).json()
    assert body["links"][0]["code"] == "tguzbeks"


async def test_a_duplicate_code_is_refused(client, registered):
    await client.post("/webapp/admin/links", headers=auth(),
                      json={"code": "dup", "trial_days": 7})
    r = await client.post("/webapp/admin/links", headers=auth(),
                          json={"code": "dup", "trial_days": 7})
    assert r.status_code == 409


async def test_a_link_cannot_be_created_beyond_the_bound(client, registered):
    r = await client.post("/webapp/admin/links", headers=auth(),
                          json={"code": "forever", "trial_days": 3650})
    assert r.status_code == 422


async def test_a_link_can_be_switched_off(client, registered, api_db):
    await client.post("/webapp/admin/links", headers=auth(),
                      json={"code": "campaign", "trial_days": 7})
    r = await client.patch("/webapp/admin/links/campaign", headers=auth(),
                           json={"active": False})
    assert r.status_code == 200
    async with api_db() as s:
        row = await s.get(ReferralLink, "campaign")
    assert row is not None and row.active is False


async def test_a_used_link_cannot_be_deleted(client, registered, api_db):
    """The invariant, restated as behaviour.

    This used to assert that NO delete route existed anywhere on the router, which is a
    proxy for the real rule and a brittle one — it broke the moment an unrelated delete was
    added, and it would have kept passing if a delete had been written that erased
    attribution by some other name. What must hold is narrower and more useful: a code that
    somebody actually came through cannot be removed, because that code is the only record
    of where they came from.
    """
    await client.post("/webapp/admin/links", headers=auth(),
                      json={"code": "brought-someone", "trial_days": 7})
    async with api_db() as s:
        s.add(User(chat_id=7100, lang="ru", source="brought-someone"))
        await s.commit()

    r = await client.delete("/webapp/admin/links/brought-someone", headers=auth())
    assert r.status_code == 409, "a link with attribution behind it was deleted"
    assert "deactivate" in r.json()["detail"], "the refusal should say what to do instead"

    async with api_db() as s:
        assert await s.get(ReferralLink, "brought-someone") is not None


async def test_an_unused_link_can_be_deleted(client, registered, api_db):
    """A link nobody used is a typo, and living with a typo for ever is its own defect."""
    await client.post("/webapp/admin/links", headers=auth(),
                      json={"code": "typpo", "trial_days": 7})
    r = await client.delete("/webapp/admin/links/typpo", headers=auth())
    assert r.status_code == 200
    async with api_db() as s:
        assert await s.get(ReferralLink, "typpo") is None


async def test_uses_are_reported(client, registered, api_db):
    await client.post("/webapp/admin/links", headers=auth(),
                      json={"code": "counted", "trial_days": 7})
    async with api_db() as s:
        s.add(User(chat_id=7001, lang="ru", source="counted"))
        await s.commit()
    body = (await client.get("/webapp/admin/links", headers=auth())).json()
    assert body["links"][0]["uses"] == 1


# --- reaching people ---------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    out = []

    async def fake(chat_id, text):
        out.append((chat_id, text))
        return True

    monkeypatch.setattr(webapp_admin.notify, "send", fake)
    from api.services import broadcast

    monkeypatch.setattr(broadcast.notify, "send", fake)
    monkeypatch.setattr(broadcast, "PAUSE", 0)
    return out


async def test_a_private_message_reaches_one_person(client, registered, sent):
    r = await client.post("/webapp/admin/message", headers=auth(),
                          json={"chat_id": OWNER, "text": "send 10 EUR to..."})
    assert r.status_code == 200
    assert sent == [(OWNER, "send 10 EUR to...")]


async def test_a_message_to_a_stranger_is_refused(client, registered, sent):
    r = await client.post("/webapp/admin/message", headers=auth(),
                          json={"chat_id": 999999, "text": "hello"})
    assert r.status_code == 404
    assert sent == []


async def test_a_broadcast_must_confirm_the_count(client, registered, sent):
    """The filter and the send are two requests, the population can change between them,
    and a newsletter cannot be unsent. A mismatch means confirming a number you were not
    shown."""
    r = await client.post("/webapp/admin/broadcast", headers=auth(),
                          json={"text": "news", "confirm_recipients": 99})
    assert r.status_code == 409
    assert sent == []


async def test_a_broadcast_with_no_confirmation_is_refused(client, registered, sent):
    r = await client.post("/webapp/admin/broadcast", headers=auth(), json={"text": "news"})
    assert r.status_code == 409
    assert sent == []


async def test_the_preview_reports_who_it_would_reach(client, registered):
    r = await client.post("/webapp/admin/broadcast/preview", headers=auth(),
                          json={"text": "news"})
    assert r.status_code == 200
    assert r.json()["recipients"] >= 1


async def test_a_confirmed_broadcast_goes_out(client, registered, sent, api_db):
    count = (await client.post("/webapp/admin/broadcast/preview", headers=auth(),
                               json={"text": "news"})).json()["recipients"]
    r = await client.post("/webapp/admin/broadcast", headers=auth(),
                          json={"text": "news", "confirm_recipients": count})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == count


async def test_a_language_filter_narrows_it(client, registered, api_db):
    async with api_db() as s:
        s.add(User(chat_id=7100, lang="uz"))
        await s.commit()
    all_of_them = (await client.post("/webapp/admin/broadcast/preview", headers=auth(),
                                     json={"text": "x"})).json()["recipients"]
    uzbek = (await client.post("/webapp/admin/broadcast/preview", headers=auth(),
                               json={"text": "x", "lang": "uz"})).json()["recipients"]
    assert uzbek < all_of_them
    assert uzbek >= 1


async def test_an_unknown_language_is_refused(client, registered):
    r = await client.post("/webapp/admin/broadcast/preview", headers=auth(),
                          json={"text": "x", "lang": "de"})
    assert r.status_code == 422


async def test_an_empty_audience_is_refused_rather_than_silently_doing_nothing(
        client, registered, api_db):
    from sqlalchemy import update as sa_update

    async with api_db() as s:
        await s.execute(sa_update(User).values(lang="ru"))
        await s.commit()
    r = await client.post("/webapp/admin/broadcast", headers=auth(),
                          json={"text": "x", "lang": "it", "confirm_recipients": 0})
    assert r.status_code == 409
