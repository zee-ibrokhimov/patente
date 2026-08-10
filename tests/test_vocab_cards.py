"""Flip cards: the mode where the answer is supposed to be in the payload.

`test_vocab.py` has a test that reads the raw JSON of a round and fails if the expected
answer appears anywhere in it. That property is right for the typing test and exactly
wrong here — a card that has to fetch its own back face puts a network round trip between
the tap and the thing the learner tapped for. So the first test below is the deliberate
inverse of that one, and it says so, because a future reader finding two tests that look
like they contradict each other deserves to know which is which.

What still has to hold:

* the answer matches the DIRECTION — an Italian-first card reveals the translation, a
  translation-first card reveals the Italian. Getting this backwards produces a card whose
  two faces are the same word;
* self-grading moves the schedule, in both directions;
* `almost` stays untouched, because a card cannot produce one;
* the paywall covers this mode too. It is the same content behind the same gate.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, User, VocabProgress, VocabTerm
from api.services import leitner
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_VOCAB_RECALL, VOCAB_IT_TO_LANG, VOCAB_LANG_TO_IT
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
]


@pytest.fixture
async def premium(client, registered, api_db):
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


async def set_lang(api_db, lang: str) -> None:
    from sqlalchemy import update as sa_update
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(lang=lang))
        await s.commit()


# --- the payload ------------------------------------------------------------

async def test_a_card_carries_its_own_answer(client, premium, api_db, terms):
    """The deliberate inverse of `test_the_round_does_not_leak_the_answer`.

    There the answer's absence is the feature; here its presence is. Both modes draw from
    the same glossary, so the difference is not about secrecy — it is that one grades
    server-side and the other cannot.
    """
    r = await client.get("/webapp/vocab/cards", headers=auth())
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "an empty deck would pass every assertion below"
    assert all(item.get("answer") for item in items)


async def test_the_answer_is_the_other_side_of_the_card(client, premium, api_db, terms):
    """Italian-first reveals the gloss; gloss-first reveals the Italian.

    Reversed, a card would show `sosta` and then reveal `sosta`. Nothing else in the
    response would look wrong.
    """
    await set_lang(api_db, "ru")
    async with api_db() as s:
        by_id = {t.id: t for t in await s.scalars(select(VocabTerm))}

    items = (await client.get("/webapp/vocab/cards", headers=auth())).json()["items"]
    for item in items:
        term = by_id[item["term_id"]]
        if item["direction"] == VOCAB_IT_TO_LANG:
            assert item["prompt"] == term.it
            assert item["answer"] == term.ru
        else:
            assert item["prompt"] == term.ru
            assert item["answer"] == term.it


async def test_the_deck_speaks_the_learners_language(client, premium, api_db, terms):
    """A card whose back face is in a language the learner does not read is a blank card."""
    await set_lang(api_db, "uz")
    items = (await client.get("/webapp/vocab/cards", headers=auth())).json()["items"]
    glosses = {t["uz"] for t in TERMS}
    italians = {t["it"] for t in TERMS}
    for item in items:
        side = item["answer"] if item["direction"] == VOCAB_IT_TO_LANG else item["prompt"]
        assert side in glosses
        other = item["prompt"] if item["direction"] == VOCAB_IT_TO_LANG else item["answer"]
        assert other in italians


# --- self-grading moves the schedule ----------------------------------------

async def test_knowing_it_promotes_the_card(client, premium, api_db, terms):
    async with api_db() as s:
        term_id = (await s.scalars(select(VocabTerm.id))).first()

    r = await client.post("/webapp/vocab/recall",
                          json={"term_id": term_id, "knew": True}, headers=auth())
    assert r.status_code == 200
    assert r.json()["box"] == 2

    async with api_db() as s:
        row = await s.get(VocabProgress, (OWNER, term_id))
    assert (row.seen, row.wrong) == (1, 0)
    # Promotion has to move the DUE DATE too. A box number that goes up while the card
    # stays due now is a progress bar, not a schedule.
    due = row.due_at if row.due_at.tzinfo else row.due_at.replace(tzinfo=timezone.utc)
    assert due >= datetime.now(timezone.utc) + leitner.interval(2) - timedelta(minutes=1)


async def test_not_knowing_it_sends_the_card_back_to_the_start(client, premium, api_db, terms):
    """Not "one box down" — all the way back, same as a wrong typed answer.

    A card you have just failed is not four days away from needing to be seen again.
    """
    async with api_db() as s:
        term_id = (await s.scalars(select(VocabTerm.id))).first()

    for _ in range(3):
        await client.post("/webapp/vocab/recall",
                          json={"term_id": term_id, "knew": True}, headers=auth())
    r = await client.post("/webapp/vocab/recall",
                          json={"term_id": term_id, "knew": False}, headers=auth())
    assert r.json()["box"] == 1

    async with api_db() as s:
        row = await s.get(VocabProgress, (OWNER, term_id))
    assert (row.seen, row.wrong) == (4, 1)


async def test_a_card_never_records_an_almost(client, premium, api_db, terms):
    """`almost` means "produced the word, missed the ending" — evidence only typing gives.

    A tap has two outcomes. Filling the column from a two-way choice would quietly corrupt
    the one statistic that distinguishes a near-miss from not knowing at all.
    """
    async with api_db() as s:
        term_id = (await s.scalars(select(VocabTerm.id))).first()

    for knew in (True, False, True):
        await client.post("/webapp/vocab/recall",
                          json={"term_id": term_id, "knew": knew}, headers=auth())

    async with api_db() as s:
        row = await s.get(VocabProgress, (OWNER, term_id))
    assert row.almost == 0


async def test_the_mode_is_recorded(client, premium, api_db, terms):
    """Recognition is weaker evidence than recall. If the vocabulary numbers ever start
    looking better than the learner does, the split has to be recoverable from the data —
    which it is not if both modes write the same event."""
    async with api_db() as s:
        term_id = (await s.scalars(select(VocabTerm.id))).first()

    await client.post("/webapp/vocab/recall",
                      json={"term_id": term_id, "knew": True}, headers=auth())

    async with api_db() as s:
        kinds = list(await s.scalars(select(Event.type)))
    assert EV_VOCAB_RECALL in kinds


async def test_an_unknown_term_is_refused(client, premium, api_db, terms):
    r = await client.post("/webapp/vocab/recall",
                          json={"term_id": 999_999, "knew": True}, headers=auth())
    assert r.status_code == 404


# --- the gate ---------------------------------------------------------------

@pytest.mark.parametrize("method, path, body", [
    ("get", "/webapp/vocab/cards", None),
    ("post", "/webapp/vocab/recall", {"term_id": 1, "knew": True}),
])
async def test_a_free_user_is_refused(client, registered, api_db, terms, method, path, body):
    """Cards are the same paid glossary in a different wrapper. A mode that forgets the
    gate hands the whole word list — answers included — to anyone who asks."""
    await end_trial(api_db, OWNER)
    call = getattr(client, method)
    r = await call(path, headers=auth(), **({"json": body} if body else {}))
    assert r.status_code == 402
