"""Words a learner adds for themselves, alongside the shared glossary.

"also can we add function so user can add a his own words for vocabulary so he can learn
better but his own vocab will be visible only for him ... so each user will have his own
vocab + our vocab"

ONE TABLE, WHICH IS THE WHOLE DESIGN AND THE WHOLE RISK.

A separate table would need its own progress rows, its own Leitner scheduling, its own place
in the round draw and the flip-card deck — four chances for the two kinds of word to behave
differently. Sharing `vocab_terms` with an `owner_chat_id` means a learner's own words are
drawn, scheduled, graded and counted by the code that already works.

The cost is that EVERY query has to be scoped, and forgetting one does not fail loudly: it
shows one learner another learner's private words. So there is a single `visible_to` helper,
and `test_no_vocab_query_forgets_the_owner` reads the module and fails on any query written
without it. That test is the load-bearing one in this file.
"""

from __future__ import annotations

import json
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update as sa_update

from api.models import User, VocabProgress, VocabTerm
from api.services import vocab as vocab_service
from api.services.telegram_auth import sign
from shared.config import settings

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
OTHER = 77
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


TERMS = [
    dict(rank=1, it="sosta", en="parking", ru="стоянка", uz="turish"),
    dict(rank=2, it="fermata", en="brief stop", ru="остановка", uz="to'xtash"),
]


@pytest.fixture
async def shared(api_db):
    async with api_db() as s:
        s.add_all([VocabTerm(**t) for t in TERMS])
        await s.commit()
    return TERMS


@pytest.fixture
async def premium(client, registered, api_db):
    async with api_db() as s:
        for chat in (OWNER, OTHER):
            await s.execute(sa_update(User).where(User.chat_id == chat).values(
                pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()
    return registered


@pytest.fixture
async def both_users(client, api_db):
    """OTHER has to exist, and be Premium, before they can own anything.

    Created the same way `registered` creates OWNER — through the users route. The first
    version posted to a GET-only endpoint, so OTHER never existed and the two tests that
    depend on a second learner failed on a missing id rather than on the thing they check.
    """
    r = await client.post("/users", json={"chat_id": OTHER, "lang": "ru"})
    assert r.status_code == 200
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OTHER).values(
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()


# --- the guarantee ----------------------------------------------------------

def test_no_vocab_query_forgets_the_owner():
    """The load-bearing test.

    A `select(VocabTerm)` without a scope shows one learner another learner's private words,
    and nothing fails — the query returns MORE rows, which no assertion elsewhere would
    notice. So the module is read, and every statement against the table has to go through
    `visible_to` or `own_only`.
    """
    src = (ROOT / "api" / "services" / "vocab.py").read_text(encoding="utf-8")
    body = src[src.index("async def "):]     # skip the helpers that define the scoping

    offenders = []
    for m in re.finditer(r"select\((?:func\.count\(\)\)\.select_from\()?VocabTerm", body):
        # The statement this belongs to: up to the next blank line or closing paren depth.
        window = body[m.start():m.start() + 400]
        if "visible_to(" not in window and "own_only(" not in window:
            line = body[:m.start()].count("\n") + 1
            offenders.append(f"~line {line}: {window.splitlines()[0].strip()}")
    assert not offenders, (
        "these read the vocabulary table without scoping it to one learner:\n"
        + "\n".join(offenders)
    )


async def test_one_learners_word_is_invisible_to_another(
        client, premium, both_users, shared, api_db):
    r = await client.post("/webapp/vocab/terms", headers=auth(OWNER),
                          json={"it": "prova", "gloss": "моё слово"})
    assert r.status_code == 201

    mine = (await client.get("/webapp/vocab/terms", headers=auth(OWNER))).json()
    theirs = (await client.get("/webapp/vocab/terms", headers=auth(OTHER))).json()

    assert "prova" in [t["it"] for t in mine["terms"]]
    assert "prova" not in [t["it"] for t in theirs["terms"]], (
        "one learner's private word reached another learner's list"
    )
    # And the shared sheet is there for both.
    assert "sosta" in [t["it"] for t in mine["terms"]]
    assert "sosta" in [t["it"] for t in theirs["terms"]]


async def test_the_counts_are_per_learner_too(client, premium, both_users, shared):
    """A total that includes somebody else's private words is a smaller leak of the same
    kind — it says how many words another learner has added."""
    await client.post("/webapp/vocab/terms", headers=auth(OWNER),
                      json={"it": "prova", "gloss": "моё"})

    mine = (await client.get("/webapp/vocab/stats", headers=auth(OWNER))).json()
    theirs = (await client.get("/webapp/vocab/stats", headers=auth(OTHER))).json()
    assert mine["total"] == len(TERMS) + 1
    assert theirs["total"] == len(TERMS)


# --- adding, editing, removing ----------------------------------------------

async def test_a_word_can_be_added_edited_and_removed(client, premium, shared, api_db):
    created = (await client.post("/webapp/vocab/terms", headers=auth(),
                                 json={"it": "prova", "gloss": "проба"})).json()
    tid = created["id"]

    r = await client.patch(f"/webapp/vocab/terms/{tid}", headers=auth(),
                           json={"gloss": "испытание"})
    assert r.status_code == 200
    assert r.json()["gloss"] == "испытание"

    r = await client.delete(f"/webapp/vocab/terms/{tid}", headers=auth())
    assert r.status_code == 204

    async with api_db() as s:
        assert await s.get(VocabTerm, tid) is None


async def test_removing_a_word_takes_its_schedule_with_it(client, premium, shared, api_db):
    """A term with no row and a Leitner schedule still pointing at it surfaces months later
    as a round containing a blank card."""
    tid = (await client.post("/webapp/vocab/terms", headers=auth(),
                             json={"it": "prova", "gloss": "проба"})).json()["id"]
    async with api_db() as s:
        s.add(VocabProgress(chat_id=OWNER, term_id=tid, box=1,
                            due_at=datetime.now(timezone.utc), seen=1, wrong=0, almost=0))
        await s.commit()

    await client.delete(f"/webapp/vocab/terms/{tid}", headers=auth())

    async with api_db() as s:
        left = list(await s.scalars(
            select(VocabProgress).where(VocabProgress.term_id == tid)))
    assert left == [], "the schedule outlived the word"


async def test_a_shared_word_cannot_be_edited_or_deleted(client, premium, shared, api_db):
    """The glossary is not ours to let one learner rewrite for everybody — and it is
    somebody else's work, credited by name."""
    async with api_db() as s:
        shared_id = (await s.scalars(
            select(VocabTerm.id).where(VocabTerm.owner_chat_id.is_(None)))).first()

    assert (await client.patch(f"/webapp/vocab/terms/{shared_id}", headers=auth(),
                               json={"gloss": "мой вариант"})).status_code == 404
    assert (await client.delete(f"/webapp/vocab/terms/{shared_id}",
                                headers=auth())).status_code == 404

    async with api_db() as s:
        assert (await s.get(VocabTerm, shared_id)) is not None


async def test_another_learners_word_is_a_404_not_a_403(
        client, premium, both_users, shared, api_db):
    """403 confirms the row exists, which already tells them something about somebody
    else's list."""
    tid = (await client.post("/webapp/vocab/terms", headers=auth(OTHER),
                             json={"it": "loro", "gloss": "их"})).json()["id"]

    assert (await client.patch(f"/webapp/vocab/terms/{tid}", headers=auth(OWNER),
                               json={"gloss": "моё"})).status_code == 404
    assert (await client.delete(f"/webapp/vocab/terms/{tid}",
                                headers=auth(OWNER))).status_code == 404


async def test_the_same_word_twice_is_refused(client, premium, shared):
    await client.post("/webapp/vocab/terms", headers=auth(),
                      json={"it": "prova", "gloss": "проба"})
    r = await client.post("/webapp/vocab/terms", headers=auth(),
                          json={"it": "PROVA", "gloss": "ещё раз"})
    assert r.status_code == 409


async def test_a_word_the_glossary_already_has_is_allowed(client, premium, shared):
    """`sosta` is in the shared sheet, and a learner may still want their own note on it.
    The old UNIQUE(it) would have refused this."""
    r = await client.post("/webapp/vocab/terms", headers=auth(),
                          json={"it": "sosta", "gloss": "моя пометка"})
    assert r.status_code == 201


async def test_empty_sides_are_refused(client, premium, shared):
    for body in ({"it": "  ", "gloss": "x"}, {"it": "x", "gloss": "  "}):
        r = await client.post("/webapp/vocab/terms", headers=auth(), json=body)
        assert r.status_code == 422, body


async def test_a_list_cannot_grow_without_bound(client, premium, shared, monkeypatch):
    monkeypatch.setattr(vocab_service, "OWN_MAX_TERMS", 2)
    for i in range(2):
        assert (await client.post("/webapp/vocab/terms", headers=auth(),
                                  json={"it": f"w{i}", "gloss": "x"})).status_code == 201
    r = await client.post("/webapp/vocab/terms", headers=auth(),
                          json={"it": "w3", "gloss": "x"})
    assert r.status_code == 422


# --- it behaves like a real word --------------------------------------------

async def test_an_added_word_is_drilled_like_any_other(client, premium, shared, api_db):
    """The reason for one table. It has to appear in a round, be answerable, and move the
    same Leitner boxes — without a line of scheduling code written for it."""
    tid = (await client.post("/webapp/vocab/terms", headers=auth(),
                             json={"it": "prova", "gloss": "проба"})).json()["id"]

    body = (await client.get("/webapp/vocab/round", headers=auth())).json()
    assert tid in [i["term_id"] for i in body["items"]], (
        "a learner's own word never came up in their own round"
    )

    r = await client.post("/webapp/vocab/answer", headers=auth(),
                          json={"term_id": tid, "direction": "it_to_lang", "given": "проба"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "correct"

    async with api_db() as s:
        row = await s.get(VocabProgress, (OWNER, tid))
    assert row is not None and row.box > 1


async def test_own_words_lead_the_learners_own_list(client, premium, shared):
    """`rank` is NULL for an addition and NULLs sort first. Somebody's own words are the
    ones they just chose to care about; burying them under 1,104 shared entries makes the
    feature pointless."""
    await client.post("/webapp/vocab/terms", headers=auth(),
                      json={"it": "zzz-mine", "gloss": "моё"})
    body = (await client.get("/webapp/vocab/terms", headers=auth())).json()
    assert body["terms"][0]["it"] == "zzz-mine", [t["it"] for t in body["terms"]]


# --- the shared sheet is not ours to damage ---------------------------------

def test_the_seeder_only_touches_the_shared_sheet():
    """`seed_vocab.py` matches on the Italian and updates glosses in place, and it runs on
    every deploy. Without the filter a re-export would find a learner's own `sosta`, decide
    it was the sheet's, and overwrite their note."""
    src = (ROOT / "content" / "seed_vocab.py").read_text(encoding="utf-8")
    assert "owner_chat_id.is_(None)" in src


def test_the_advertised_glossary_size_is_the_shared_one():
    """"1,104 exam words" is what the product claims to contain. Counting a learner's own
    additions into it would make the advertised size different for every user."""
    src = (ROOT / "api" / "routes" / "users.py").read_text(encoding="utf-8")
    block = src[src.index("vocab_terms = "):][:400]
    assert "owner_chat_id.is_(None)" in block


# --- the client -------------------------------------------------------------

def _main() -> str:
    return (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")


def test_the_add_control_is_above_the_list():
    """"adding function should be in the first" — above the words, not below a thousand
    rows where nobody scrolls to."""
    src = _main()
    block = src[src.index("function vocabList("):]
    block = block[:block.index("\n\n\n")]
    add_at = block.index('el("button", "v-add")')
    list_at = block.index('el("div", "v-list")')
    assert add_at < list_at, "the add control renders after the list"
    assert "openOwnWord(null)" in block


def test_only_a_learners_own_rows_offer_editing():
    """A shared entry belongs to the person who compiled the glossary and is the same for
    everybody. Offering an edit on it would be offering to change somebody else's work."""
    src = _main()
    block = src[src.index("function vocabList("):]
    block = block[:block.index("\n\n\n")]
    assert "if (term.mine)" in block
    edit = block[block.index("if (term.mine)"):block.index("list.append(row)")]
    assert "openOwnWord(term)" in edit


def test_deleting_asks_first_and_names_the_word():
    """It takes the Leitner history with it, which is not obvious from a bin icon."""
    src = _main()
    block = src[src.index("function openOwnWord("):]
    block = block[:block.index("\nfunction ")]
    assert "v_remove_confirm" in block
    assert "existing.it" in block, "the confirmation should name the word being removed"


def test_the_list_is_re_read_rather_than_patched():
    """The server decides the order and a new word belongs at the top of it. Splicing the
    row in client-side would put it wherever the client guessed."""
    src = _main()
    block = src[src.index("function openOwnWord("):]
    block = block[:block.index("\nfunction ")]
    assert "loadVocabList(state.vocab.query)" in block


@pytest.mark.parametrize("key", ["v_add", "v_edit", "v_add_sub", "v_add_it", "v_add_gloss",
                                 "v_save", "v_remove", "v_remove_confirm"])
def test_every_language_has_the_words_for_it(key):
    i18n = (ROOT / "webapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    assert i18n.count(f"{key}:") == 4
