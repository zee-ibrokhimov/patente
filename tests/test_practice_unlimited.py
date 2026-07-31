"""Practice runs until the learner stops it; an exam is thirty questions.

The bug this replaces was a ternary with the same value in both branches:

    count = EXAM_QUESTIONS if is_exam else EXAM_QUESTIONS

so practice silently ended after thirty and looked deliberate. The tests here pin both
halves — that practice keeps going, and that an exam categorically cannot be extended,
because an exam whose length the client can change reports a score that means nothing.
"""

from __future__ import annotations

import json
import time

import pytest

from api.models import Question, Quesito, QuizSession, QuizSessionItem
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EXAM_QUESTIONS, MODE_EXAM, MODE_PRACTICE, PRACTICE_BATCH

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


@pytest.fixture
async def many_questions(api_db):
    """Enough questions that a batch boundary is reachable in a test.

    The shared fixture has four; practice draws PRACTICE_BATCH at a time, so without
    this the first batch would be the whole bank and "does it extend" could not be asked.
    """
    async with api_db() as s:
        s.add(Quesito(id=300, topic_id=1, primary_image=None))
        await s.flush()
        s.add_all([
            Question(id=1000 + i, quesito_id=300, topic_id=1, cluster_id=1,
                     statement_it=f"Affermazione numero {i}", answer=i % 2 == 0,
                     source_version="v1")
            for i in range(PRACTICE_BATCH * 3)
        ])
        await s.commit()


async def start(client, mode: str):
    return await client.post("/webapp/sessions", headers=auth(), json={"mode": mode})


# --- the sizes are different ------------------------------------------------

async def test_practice_and_exam_no_longer_draw_the_same_count(
        client, registered, many_questions):
    """The original defect in one assertion: both branches returned EXAM_QUESTIONS."""
    practice = (await start(client, MODE_PRACTICE)).json()
    exam = (await start(client, MODE_EXAM)).json()
    assert exam["question_count"] == EXAM_QUESTIONS
    assert practice["question_count"] == PRACTICE_BATCH


async def test_practice_extends_when_it_runs_out(client, registered, many_questions):
    started = (await start(client, MODE_PRACTICE)).json()
    first = started["question_count"]

    r = await client.post(f"/webapp/sessions/{started['id']}/extend", headers=auth())
    assert r.status_code == 200
    assert r.json()["question_count"] == first + PRACTICE_BATCH


async def test_extending_returns_the_whole_paper_including_the_new_items(
        client, registered, many_questions):
    """The client replaces its paper with this response, so a partial one would drop
    every question the learner has already seen and break resume."""
    started = (await start(client, MODE_PRACTICE)).json()
    body = (await client.post(f"/webapp/sessions/{started['id']}/extend",
                              headers=auth())).json()
    assert len(body["questions"]) == body["question_count"]


async def test_extending_never_repeats_a_question_from_the_same_sitting(
        client, registered, many_questions):
    started = (await start(client, MODE_PRACTICE)).json()
    body = (await client.post(f"/webapp/sessions/{started['id']}/extend",
                              headers=auth())).json()
    ids = [q["id"] for q in body["questions"]]
    assert len(ids) == len(set(ids)), "a question was served twice in one sitting"


async def test_practice_can_be_extended_repeatedly(client, registered, many_questions):
    """"Unlimited" means more than once."""
    started = (await start(client, MODE_PRACTICE)).json()
    count = started["question_count"]
    for _ in range(2):
        body = (await client.post(f"/webapp/sessions/{started['id']}/extend",
                                  headers=auth())).json()
        assert body["question_count"] > count
        count = body["question_count"]


async def test_running_out_of_bank_is_not_an_error(client, registered):
    """With only the fixture's four questions there is nothing left to add. The sitting
    stays open so the learner can end it themselves, rather than erroring at them."""
    started = (await start(client, MODE_PRACTICE)).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/extend", headers=auth())
    assert r.status_code == 200
    assert r.json()["state"] == "open"


# --- an exam is thirty questions, and stays thirty --------------------------

async def test_an_exam_cannot_be_extended(client, registered, many_questions):
    """The load-bearing one. An exam simulates a real thirty-question paper; if the
    client can lengthen it, the pass/fail it reports is meaningless."""
    started = (await start(client, MODE_EXAM)).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/extend", headers=auth())
    assert r.status_code == 409
    assert (await client.get(f"/webapp/sessions/{started['id']}",
                             headers=auth())).json()["question_count"] == EXAM_QUESTIONS


async def test_a_finished_sitting_cannot_be_extended(client, registered, many_questions):
    """Otherwise a graded practice run could be reopened and its result changed."""
    started = (await start(client, MODE_PRACTICE)).json()
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())
    r = await client.post(f"/webapp/sessions/{started['id']}/extend", headers=auth())
    assert r.status_code == 409


async def test_extending_someone_elses_sitting_is_refused(
        client, registered, many_questions):
    """Session ids are small integers on a public surface."""
    started = (await start(client, MODE_PRACTICE)).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/extend", headers=auth(99))
    assert r.status_code in (403, 404)


async def test_extending_still_requires_a_signature(client, registered, many_questions):
    started = (await start(client, MODE_PRACTICE)).json()
    assert (await client.post(f"/webapp/sessions/{started['id']}/extend")).status_code == 401
