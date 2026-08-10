"""The suggestion form: what a learner can send, and what the owner gets.

The first version of "what should we add?" opened the support chat. The owner rejected it,
and he was right: a chat link asks somebody to compose a message to a stranger — which
almost nobody does — and it drops "add a dark mode" into the same inbox as "my payment
failed".

So it is a form. Which makes this the one endpoint in the product that stores free text a
user typed, and most of what follows is about that: a bound on length, a bound on volume,
and a deliberate account of what is NOT stored alongside it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Suggestion, User
from api.services import suggestions
from api.services.telegram_auth import sign
from shared.config import settings

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
STAFF = 42


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


@pytest.fixture
async def staff(api_db, registered, monkeypatch):
    """OWNER is staff; nobody else is. Same knob the admin-panel tests use."""
    monkeypatch.setattr(settings, "admin_chat_ids", str(STAFF))
    return registered


# --- sending ----------------------------------------------------------------

async def test_a_suggestion_is_stored_with_its_language(client, registered, api_db):
    """The owner reads four languages' worth of these and needs to know which one a message
    is in before he opens it."""
    r = await client.post("/webapp/suggestions", headers=auth(),
                          json={"text": "добавьте тёмную тему"})
    assert r.status_code == 201

    async with api_db() as s:
        row = (await s.scalars(select(Suggestion))).one()
    assert row.text == "добавьте тёмную тему"
    assert row.lang == "ru"
    assert row.chat_id == OWNER
    assert row.handled_at is None


async def test_nothing_beyond_the_message_is_kept(client, registered, api_db):
    """Who wrote it, what they wrote, and which language — and that is the whole record.

    Stated as a test because the cheapest thing in the world is to add a column for
    "screen" or "app version" while you are here, and the reason not to is that it would be
    collected because it was easy rather than because it was needed.
    """
    columns = {c.name for c in Suggestion.__table__.columns}
    assert columns == {"id", "chat_id", "text", "lang", "created_at", "handled_at"}, columns


async def test_an_empty_message_is_refused(client, registered):
    """Whitespace only. The empty string is caught by the schema's min_length before it ever
    reaches the service; these are the ones that look like text and are not."""
    for text in ("   ", "\n\t "):
        r = await client.post("/webapp/suggestions", headers=auth(), json={"text": text})
        assert r.status_code == 422, text


async def test_a_very_long_message_is_refused_at_the_door(client, registered, api_db):
    """The schema catches it before the service is reached, and nothing is written."""
    r = await client.post("/webapp/suggestions", headers=auth(),
                          json={"text": "x" * (suggestions.MAX_LENGTH + 1)})
    assert r.status_code == 422

    async with api_db() as s:
        assert list(await s.scalars(select(Suggestion))) == []


async def test_the_service_refuses_rather_than_truncating(api_db):
    """The HTTP test above passes for the WRONG reason — Pydantic's max_length rejects the
    body before `submit` ever runs, so it says nothing about what the service does.

    Mutation found that: replacing the service's refusal with a silent truncation broke
    nothing. It matters because the service is callable from elsewhere, and silently cutting
    off somebody's last sentence is worse than telling them — they cannot tell it happened.
    """
    async with api_db() as s:
        with pytest.raises(suggestions.Refused):
            await suggestions.submit(s, OWNER, "x" * (suggestions.MAX_LENGTH + 1), "ru")
        assert list(await s.scalars(select(Suggestion))) == []


async def test_a_day_has_a_limit_but_a_generous_one(client, registered, monkeypatch):
    """A learner with three ideas in an evening is exactly who this is for; an hour of
    pasting is not."""
    monkeypatch.setattr(suggestions, "DAILY_LIMIT", 3)
    for i in range(3):
        r = await client.post("/webapp/suggestions", headers=auth(),
                              json={"text": f"idea {i}"})
        assert r.status_code == 201

    r = await client.post("/webapp/suggestions", headers=auth(), json={"text": "one more"})
    assert r.status_code == 422


async def test_one_learners_limit_is_not_another_s(client, registered, monkeypatch):
    monkeypatch.setattr(suggestions, "DAILY_LIMIT", 1)
    await client.post("/webapp/suggestions", headers=auth(), json={"text": "mine"})
    assert (await client.post("/webapp/suggestions", headers=auth(),
                              json={"text": "again"})).status_code == 422

    other = await client.post("/webapp/suggestions", headers=auth(chat_id=99_002),
                              json={"text": "theirs"})
    assert other.status_code == 201


# --- reading ----------------------------------------------------------------

async def test_the_owner_reads_unhandled_first(client, staff, api_db):
    async with api_db() as s:
        old = datetime.now(timezone.utc) - timedelta(days=2)
        s.add_all([
            Suggestion(chat_id=1, text="handled and old", lang="ru",
                       created_at=old, handled_at=old),
            Suggestion(chat_id=2, text="open and older", lang="en",
                       created_at=old - timedelta(days=1)),
        ])
        await s.commit()

    body = (await client.get("/webapp/admin/suggestions", headers=auth())).json()
    assert [x["text"] for x in body["suggestions"]] == ["open and older", "handled and old"]
    assert body["open"] == 1


async def test_marking_one_handled_moves_it_down(client, staff, api_db):
    async with api_db() as s:
        s.add(Suggestion(chat_id=1, text="something", lang="ru"))
        await s.commit()
        sid = (await s.scalars(select(Suggestion.id))).one()

    r = await client.post(f"/webapp/admin/suggestions/{sid}/handled", headers=auth())
    assert r.status_code == 200

    body = (await client.get("/webapp/admin/suggestions", headers=auth())).json()
    assert body["open"] == 0
    assert body["suggestions"][0]["handled"] is True


async def test_a_learner_cannot_read_the_queue(client, registered, api_db):
    """It carries other people's messages and their chat ids. The admin surface answers 404
    rather than 403 to anyone who is not staff — see the staff_user dependency."""
    r = await client.get("/webapp/admin/suggestions", headers=auth())
    assert r.status_code == 404


# --- the client -------------------------------------------------------------

def test_settings_opens_a_form_and_not_a_chat_link():
    """The change the owner asked for. A chat link is not a form, and the difference is
    whether anyone actually sends anything."""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    block = src[src.index("function settingsScreen("):]
    block = block[:block.index("\nfunction ")]
    head = block[:block.index("--- language ---")]
    assert "openSuggestion" in head
    assert "openChat" not in head, "the suggestion box still opens a chat"

    form = src[src.index("function openSuggestion("):]
    form = form[:form.index("\nfunction ")]
    assert "api.suggest(" in form
    assert "textarea" in form
    # The sent state, in place of the box. A toast fades in three seconds and leaves the
    # person looking at the same empty field wondering whether it went.
    assert "suggest_thanks" in form
