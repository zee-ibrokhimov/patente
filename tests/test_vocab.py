"""The vocabulary trainer, end to end.

Two properties carry real weight here and the rest is arithmetic.

**The round must not ship the answer.** The obvious implementation hands the client a
paper with both sides of each card and lets it grade locally — fast, offline-capable, and
completely useless as a test, because the answers are readable in the network tab. There
is a test below that reads the raw JSON and fails if the expected answer appears anywhere
in it, so the property is checked against the wire format rather than against intent.

**Premium means Premium.** Vocabulary is the feature four locales have been advertising
as "coming soon", and it is the one being sold. A gate that a client can decline to
enforce is not a gate, so every route is checked against a user who is genuinely past
their trial.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import User, VocabProgress, VocabTerm
from api.services import vocab as vocab_service
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT
from tests.conftest import end_trial

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42


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
    dict(rank=2, it="fermata", en="brief stop", ru="кратковременная остановка",
         uz="qisqa to'xtash"),
    dict(rank=3, it="sorpasso", en="overtaking", ru="обгон", uz="quvib o'tish"),
    dict(rank=4, it="carreggiata", en="carriageway", ru="проезжая часть",
         uz="qatnov qismi"),
    dict(rank=5, it="avvisatore acustico", en="horn", ru="звуковой сигнал, клаксон",
         uz="signal, klakson"),
]


@pytest.fixture
async def premium(client, registered, api_db):
    """A user who has actually paid.

    `registered` is deliberately the free baseline — it ends the trial — so every test
    that exercises the feature rather than the paywall needs this instead.
    """
    from sqlalchemy import update as sa_update
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()
    return registered


@pytest.fixture
async def terms(api_db):
    async with api_db() as s:
        s.add_all([VocabTerm(**t) for t in TERMS])
        await s.commit()
    return TERMS


async def set_lang(api_db, lang: str, chat_id: int = OWNER) -> None:
    from sqlalchemy import update as sa_update
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == chat_id).values(lang=lang))
        await s.commit()


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", [
    ("get", "/webapp/vocab/round", None),
    ("get", "/webapp/vocab/terms", None),
    ("post", "/webapp/vocab/answer", {"term_id": 1, "direction": VOCAB_IT_TO_LANG,
                                      "given": "parking"}),
])
async def test_a_free_user_is_refused(client, registered, api_db, terms, method, path, body):
    await end_trial(api_db, OWNER)
    call = getattr(client, method)
    r = await call(path, headers=auth(), **({"json": body} if body else {}))
    assert r.status_code == 402


async def test_a_channel_member_is_admitted(client, premium, api_db, terms):
    """Premium now has three sources — a pass, channel membership, or staff — and the
    newest paid feature must honour all of them. A feature that only checks one is
    Premium on one screen and not on the next.

    The internal trial no longer exists (Tribute owns it), so this used to be the
    "on the trial" case and is now the channel case."""
    from sqlalchemy import update as sa_update

    from datetime import datetime, timezone

    from api.models import User
    async with api_db() as s:
        # channel_checked_at set to NOW deliberately: a stale timestamp would schedule
        # the background refresh, which opens its own session from DATABASE_URL and so
        # would reach past the test database into the real one.
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            pass_expires_at=None, channel_status="member",
            channel_checked_at=datetime.now(timezone.utc)))
        await s.commit()

    r = await client.get("/webapp/vocab/round", headers=auth())
    assert r.status_code == 200
    assert r.json()["size"] > 0


async def test_stats_stay_outside_the_paywall(client, registered, api_db, terms):
    """The number that makes the feature worth buying must be visible to someone who has
    not bought it."""
    await end_trial(api_db, OWNER)
    r = await client.get("/webapp/vocab/stats", headers=auth())
    assert r.status_code == 200
    assert r.json()["total"] == len(TERMS)


async def test_every_vocab_route_demands_a_signature(client, terms):
    for path in ("/webapp/vocab/round", "/webapp/vocab/terms", "/webapp/vocab/stats"):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/webapp/vocab/answer",
                              json={"term_id": 1, "direction": VOCAB_IT_TO_LANG,
                                    "given": "x"})).status_code == 401


# --- the round must not give the game away ---------------------------------

async def test_the_round_never_ships_the_expected_answer(client, premium, api_db, terms):
    """Checked against the raw response body, not the parsed model: a future field that
    happened to carry the answer would pass a structural assertion and fail this one."""
    await set_lang(api_db, "ru")
    r = await client.get("/webapp/vocab/round", headers=auth())
    raw = r.text

    for item, term in ((i, t) for i in r.json()["items"] for t in TERMS
                       if t["it"] == i["prompt"] or t["ru"] == i["prompt"]):
        answer = term["ru"] if item["direction"] == VOCAB_IT_TO_LANG else term["it"]
        assert answer not in raw, f"the round leaked the answer {answer!r}"


async def test_the_round_mixes_both_directions(client, premium, terms):
    r = await client.get("/webapp/vocab/round", headers=auth())
    directions = {i["direction"] for i in r.json()["items"]}
    assert directions == {VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT}


async def test_the_round_says_which_language_to_answer_in(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    r = await client.get("/webapp/vocab/round", headers=auth())
    for i in r.json()["items"]:
        assert i["answer_lang"] == ("ru" if i["direction"] == VOCAB_IT_TO_LANG else "it")


# --- grading and progression ------------------------------------------------

async def answer(client, term_id: int, direction: str, given: str):
    return await client.post("/webapp/vocab/answer", headers=auth(),
                             json={"term_id": term_id, "direction": direction,
                                   "given": given})


async def test_a_right_answer_is_correct_and_advances_the_box(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    r = await answer(client, 1, VOCAB_IT_TO_LANG, "стоянка")
    assert r.json()["verdict"] == "correct"
    assert r.json()["box"] == 2


async def test_the_other_direction_expects_the_italian(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    r = await answer(client, 1, VOCAB_LANG_TO_IT, "sosta")
    assert r.json()["verdict"] == "correct"


async def test_a_grammatical_slip_is_almost_and_carries_the_correction(
        client, premium, api_db, terms):
    """The behaviour the owner asked for: not wrong, and shown the right form."""
    await set_lang(api_db, "ru")
    r = await answer(client, 3, VOCAB_LANG_TO_IT, "sorpassa")   # sorpasso
    body = r.json()
    assert body["verdict"] == "almost"
    assert body["correction"] == "sorpasso"


async def test_almost_still_advances_the_box(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    body = (await answer(client, 3, VOCAB_LANG_TO_IT, "sorpassa")).json()
    assert body["box"] == 2, "a near-miss should not send a known word back to box one"


async def test_almost_is_counted_separately_from_wrong(client, premium, api_db, terms):
    """`almost` and `wrong` answer different questions — is this learner failing on
    vocabulary, or on grammar? Merging them loses that."""
    await set_lang(api_db, "ru")
    await answer(client, 3, VOCAB_LANG_TO_IT, "sorpassa")
    async with api_db() as s:
        row = await s.get(VocabProgress, (OWNER, 3))
        assert (row.almost, row.wrong) == (1, 0)


async def test_a_wrong_answer_resets_to_box_one_and_still_shows_the_answer(
        client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    for _ in range(2):
        await answer(client, 1, VOCAB_IT_TO_LANG, "стоянка")     # climb to box 3
    body = (await answer(client, 1, VOCAB_IT_TO_LANG, "обгон")).json()
    assert body["verdict"] == "wrong"
    assert body["box"] == 1
    assert body["expected"] == "стоянка", "the learner must still be told the answer"


async def test_either_stored_alternative_is_accepted(client, premium, api_db, terms):
    """`звуковой сигнал, клаксон` offers two right answers and neither is more right."""
    await set_lang(api_db, "ru")
    assert (await answer(client, 5, VOCAB_IT_TO_LANG, "клаксон")).json()["verdict"] == "correct"
    assert (await answer(client, 5, VOCAB_IT_TO_LANG, "звуковой сигнал")).json()["verdict"] == "correct"


async def test_the_legally_distinct_pair_is_marked_wrong_not_almost(
        client, premium, api_db, terms):
    """sosta answered as fermata is the confusion this whole app exists to prevent.
    Grading it as a near-miss would teach it."""
    await set_lang(api_db, "ru")
    body = (await answer(client, 1, VOCAB_LANG_TO_IT, "fermata")).json()
    assert body["verdict"] == "wrong"


async def test_an_unknown_direction_is_refused(client, premium, terms):
    r = await answer(client, 1, "sideways", "sosta")
    assert r.status_code == 422


async def test_an_unknown_term_is_refused(client, premium, terms):
    r = await answer(client, 9999, VOCAB_IT_TO_LANG, "sosta")
    assert r.status_code == 404


# --- the word list ----------------------------------------------------------

async def test_the_list_is_searchable_in_both_languages(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    by_it = await client.get("/webapp/vocab/terms", params={"q": "sorp"}, headers=auth())
    assert [t["it"] for t in by_it.json()["terms"]] == ["sorpasso"]

    by_ru = await client.get("/webapp/vocab/terms", params={"q": "обгон"}, headers=auth())
    assert [t["it"] for t in by_ru.json()["terms"]] == ["sorpasso"]


async def test_the_list_comes_in_teaching_order(client, premium, terms):
    r = await client.get("/webapp/vocab/terms", headers=auth())
    ranks = [t["rank"] for t in r.json()["terms"]]
    assert ranks == sorted(ranks)


async def test_the_list_shows_how_well_each_word_is_known(client, premium, api_db, terms):
    await set_lang(api_db, "ru")
    await answer(client, 1, VOCAB_IT_TO_LANG, "стоянка")
    r = await client.get("/webapp/vocab/terms", headers=auth())
    boxes = {t["it"]: t["box"] for t in r.json()["terms"]}
    assert boxes["sosta"] == 2
    assert boxes["fermata"] == 0, "never answered should read as box 0, not box 1"


# --- language pairing -------------------------------------------------------

@pytest.mark.parametrize("lang, expected", [
    ("ru", "ru"), ("en", "en"), ("uz", "uz"),
    ("it", "en"),   # it/it is not a question
    ("de", "en"),   # anything unrecognised degrades rather than raising
])
def test_the_pair_language_never_ends_up_italian(lang, expected):
    assert vocab_service.pair_language(User(chat_id=1, lang=lang)) == expected


async def test_an_italian_speaker_is_tested_against_english(client, premium, api_db, terms):
    await set_lang(api_db, "it")
    r = await client.get("/webapp/vocab/round", headers=auth())
    assert r.json()["lang"] == "en"


# --- what the round chooses -------------------------------------------------

async def test_due_words_come_before_new_ones(client, premium, api_db, terms):
    """Someone with words about to be forgotten should be shown those, not fresh ones."""
    past = datetime.now(timezone.utc) - timedelta(days=1)
    async with api_db() as s:
        s.add(VocabProgress(chat_id=OWNER, term_id=4, box=2, due_at=past, seen=1))
        await s.commit()

    r = await client.get("/webapp/vocab/round", headers=auth())
    prompts = [i["prompt"] for i in r.json()["items"]]
    assert "carreggiata" in prompts or "проезжая часть" in prompts


async def test_a_round_is_capped_at_the_configured_size(client, premium, api_db):
    async with api_db() as s:
        s.add_all([VocabTerm(rank=100 + i, it=f"parola{i}", en=f"word{i}",
                             ru=f"слово{i}", uz=f"soz{i}") for i in range(50)])
        await s.commit()
    r = await client.get("/webapp/vocab/round", headers=auth())
    from shared.constants import VOCAB_ROUND_SIZE
    assert r.json()["size"] == VOCAB_ROUND_SIZE


async def test_stats_count_a_word_as_learned_only_once_it_has_stuck(
        client, premium, api_db, terms):
    """Box 2 means "got it right once", which nobody would call knowing a word."""
    async with api_db() as s:
        s.add(VocabProgress(chat_id=OWNER, term_id=1, box=2,
                            due_at=datetime.now(timezone.utc), seen=1))
        s.add(VocabProgress(chat_id=OWNER, term_id=2, box=4,
                            due_at=datetime.now(timezone.utc), seen=6))
        await s.commit()
    body = (await client.get("/webapp/vocab/stats", headers=auth())).json()
    assert body["started"] == 2
    assert body["learned"] == 1


# --- more than one Italian word can be right --------------------------------
#
# Italian shares glosses constantly. `conducente` and `autista` are both "водитель";
# `preavvisa` and `avverte` are both "warns". A reverse card showed one gloss and accepted
# only the word we happened to have picked, so a learner typing a genuinely correct
# translation was told they were wrong.
#
# Measured on the shipped list: 105 Italian words share a Russian gloss, 117 an English
# one, 177 an Uzbek one. That is 10-16% of every reverse card calling a right answer wrong,
# and being told you are wrong when you are right is the fastest way to stop trusting a
# trainer.


@pytest.fixture
async def synonyms(api_db):
    """Two Italian words, one Russian meaning."""
    async with api_db() as s:
        s.add_all([
            VocabTerm(rank=90, it="conducente", en="driver", ru="водитель", uz="haydovchi"),
            VocabTerm(rank=91, it="autista", en="driver", ru="водитель", uz="haydovchi"),
        ])
        await s.commit()
    return api_db


async def test_a_synonym_is_accepted_on_the_reverse_card(client, premium, api_db, synonyms):
    """THE fix. The card asks about `conducente`; `autista` is also "водитель"."""
    await set_lang(api_db, "ru")
    async with api_db() as s:
        asked = (await s.scalars(select(VocabTerm).where(VocabTerm.it == "conducente"))).one()

    body = (await answer(client, asked.id, VOCAB_LANG_TO_IT, "autista")).json()
    assert body["verdict"] == "correct", "a correct translation was marked wrong"


async def test_the_correction_still_names_the_word_that_was_asked(
        client, premium, api_db, synonyms):
    """Correcting someone to a synonym they were never shown answers a question nobody
    asked. A wrong answer is corrected to the card's own word."""
    await set_lang(api_db, "ru")
    async with api_db() as s:
        asked = (await s.scalars(select(VocabTerm).where(VocabTerm.it == "conducente"))).one()

    body = (await answer(client, asked.id, VOCAB_LANG_TO_IT, "qwerty")).json()
    assert body["verdict"] == "wrong"
    assert body["expected"] == "conducente"


async def test_a_near_miss_on_a_synonym_is_still_a_near_miss(client, premium, api_db, synonyms):
    """`autist` for `autista` is the right word in the wrong form, even though the card
    asked about `conducente`. Grading it WRONG would punish someone who knew more than
    the card expected."""
    await set_lang(api_db, "ru")
    async with api_db() as s:
        asked = (await s.scalars(select(VocabTerm).where(VocabTerm.it == "conducente"))).one()

    body = (await answer(client, asked.id, VOCAB_LANG_TO_IT, "autist")).json()
    assert body["verdict"] == "almost"


async def test_a_genuinely_different_word_is_still_wrong(client, premium, api_db, synonyms):
    """Accepting synonyms must not become accepting anything."""
    await set_lang(api_db, "ru")
    async with api_db() as s:
        asked = (await s.scalars(select(VocabTerm).where(VocabTerm.it == "conducente"))).one()

    body = (await answer(client, asked.id, VOCAB_LANG_TO_IT, "fermata")).json()
    assert body["verdict"] == "wrong"


async def test_the_forward_direction_is_unaffected(client, premium, api_db, synonyms):
    """it -> lang has no ambiguity to resolve: the Italian is given and one gloss is
    expected. This must not have loosened."""
    await set_lang(api_db, "ru")
    async with api_db() as s:
        asked = (await s.scalars(select(VocabTerm).where(VocabTerm.it == "conducente"))).one()

    assert (await answer(client, asked.id, VOCAB_IT_TO_LANG, "водитель")).json()["verdict"] == "correct"
    assert (await answer(client, asked.id, VOCAB_IT_TO_LANG, "стоянка")).json()["verdict"] == "wrong"
