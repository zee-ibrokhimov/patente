"""The vocabulary is shown in the language the learner set.

Reported from a screenshot: an English interface — Home / Profile / Stats / Settings —
listing `carreggiata → qatnov qismi` and `corsia → yo'lak`. Uzbek glosses under English
chrome.

THE SERVER WAS NEVER WRONG. `vocab.pair_language(user)` reads `user.lang`, and every vocab
endpoint uses it, so the API returns exactly the right column. The bug is in the client's
cache lifetime:

  · `state.vocab.list` holds the fetched word list;
  · changing the language cleared `state.stats` and `state.profile` and stopped there;
  · `openVocab` rebuilds its state with `{ ...state.vocab, round: null, ... }`, and that
    spread PRESERVES `list`, `stats` and `query`.

So: open the word list in Uzbek, switch the app to English, come back — English labels,
Uzbek words, straight from a cache nobody invalidated.

These tests cover the server contract the fix leans on. The client half is asserted on the
source, because the vocabulary screen has no DOM harness here and an assertion that can only
be checked by hand is worse than one pinned to the code.
"""

from __future__ import annotations

import json
import pathlib
import re
import time

import pytest
from sqlalchemy import update as sa_update

from api.models import User
from api.services import vocab as vocab_service
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import TRANSLATION_LANGUAGES, VOCAB_PAIR_FALLBACK

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
MAIN = (pathlib.Path(__file__).resolve().parent.parent
        / "webapp/src/main.ts").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def _set_lang(api_db, lang: str):
    from datetime import datetime, timedelta, timezone

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            lang=lang,
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()


# --- the server contract ----------------------------------------------------

@pytest.mark.parametrize("lang", TRANSLATION_LANGUAGES)
async def test_the_list_is_returned_in_the_users_language(client, registered, api_db, lang):
    await _set_lang(api_db, lang)
    body = (await client.get("/webapp/vocab/terms", headers=auth())).json()
    assert body["lang"] == lang


async def test_the_list_declares_its_own_language(client, registered, api_db):
    """The field the client checks. Without it a stale cache is undetectable — the words
    themselves are the only clue, and the client cannot read Uzbek."""
    await _set_lang(api_db, "uz")
    body = (await client.get("/webapp/vocab/terms", headers=auth())).json()
    assert "lang" in body


async def test_changing_language_changes_what_the_server_returns(client, registered, api_db):
    """The precondition for the whole bug being a CLIENT bug."""
    await _set_lang(api_db, "uz")
    uz = (await client.get("/webapp/vocab/terms", headers=auth())).json()
    await _set_lang(api_db, "en")
    en = (await client.get("/webapp/vocab/terms", headers=auth())).json()

    assert uz["lang"] == "uz" and en["lang"] == "en"
    if uz["terms"] and en["terms"]:
        assert uz["terms"][0]["gloss"] != en["terms"][0]["gloss"], \
            "the two languages returned the same gloss; this test proves nothing"


async def test_an_italian_learner_is_tested_against_english(client, registered, api_db):
    """An it/it pair is not a question, so Italian falls back — the same reason Italian is
    absent from TRANSLATION_LANGUAGES."""
    await _set_lang(api_db, "it")
    body = (await client.get("/webapp/vocab/terms", headers=auth())).json()
    assert body["lang"] == VOCAB_PAIR_FALLBACK


def test_pair_language_never_returns_italian():
    class Fake:
        lang = "it"

    assert vocab_service.pair_language(Fake()) != "it"


# --- the client half, pinned on the source ----------------------------------

def test_changing_the_language_drops_the_localised_caches():
    """`state.stats` and `state.profile` were cleared and the vocabulary was not — which is
    the one cache whose CONTENT is in the language, not just its labels."""
    assert "function dropLocalisedCaches" in MAIN
    body = MAIN[MAIN.index("function dropLocalisedCaches"):]
    body = body[:body.index("\nfunction ")]
    for field in ("list:", "stats:", "round:", "query:"):
        assert field in body, f"dropLocalisedCaches leaves vocab {field} in place"


def test_the_language_switch_calls_it():
    switch = MAIN[MAIN.index("api.settings({ lang: code })"):]
    switch = switch[:switch.index("};")]
    assert "dropLocalisedCaches()" in switch, \
        "changing the language no longer clears the caches it invalidates"


def test_entering_the_vocabulary_rejects_a_list_from_another_language():
    """The belt-and-braces half: the language can also change from the BOT while the app is
    open, which no in-app handler would see."""
    entry = MAIN[MAIN.index("async function openVocab"):]
    entry = entry[:entry.index("\nasync function loadVocabStats")]
    assert re.search(r"state\.vocab\.list\.lang !== state\.me\?\.lang", entry), \
        "openVocab keeps a cached list without checking which language it is in"


def test_the_spread_that_caused_it_is_documented():
    """`{ ...state.vocab }` preserving `list` is the mechanism, and it looks harmless. The
    comment is what stops it being reintroduced by someone tidying the reset."""
    entry = MAIN[MAIN.index("async function openVocab"):]
    entry = entry[:entry.index("\nasync function loadVocabStats")]
    assert "language-dependent" in entry.lower()
