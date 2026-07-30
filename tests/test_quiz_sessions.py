"""Exam and practice sittings.

The exam is a measurement, and every test here defends one of the three properties that
makes it one: the clock belongs to the server, nothing is revealed until the end, and a
sitting belongs to exactly one caller.

The two that would be invisible in production if they broke are the taster spend and the
Leitner writes — both are silent, both are unrepairable after the fact, and neither
shows up as an error.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Progress, QuizSession, User
from api.services import quiz_sessions
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    EV_PAYWALL_HIT,
    MODE_EXAM,
    MODE_PRACTICE,
    SESSION_EXPIRED,
    SESSION_OPEN,
)

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
INTRUDER = 999999


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {
        "X-Telegram-Init-Data": sign(
            {
                "user": json.dumps({"id": chat_id, "language_code": "ru"}, separators=(",", ":")),
                "auth_date": str(int(time.time())),
            },
            TOKEN,
        )
    }


async def start(client, mode=MODE_EXAM, who=OWNER):
    r = await client.post("/webapp/sessions", headers=auth(who), json={"mode": mode})
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------
# the paper
# --------------------------------------------------------------------------

async def test_starting_an_exam_returns_the_whole_paper_in_one_response(client, registered):
    """The paper is frozen at creation and QuestionOut carries no answer key, so shipping
    it whole is safe - and it removes 30 blocking round trips from a screen with a clock
    running on it."""
    s = await start(client)
    assert s["mode"] == MODE_EXAM
    assert len(s["questions"]) == s["question_count"] > 0
    assert s["expires_at"] is not None
    assert s["server_now"] is not None


async def test_the_paper_never_repeats_a_question(client, registered):
    """Serving one at a time would re-serve the exam's own misses: box 1 is 10 minutes,
    the exam runs 20, and selection orders strictly by due_at."""
    s = await start(client)
    ids = [q["id"] for q in s["questions"]]
    assert len(set(ids)) == len(ids)


async def test_the_paper_carries_no_answer_key(client, registered):
    s = await start(client)
    blob = json.dumps(s["questions"])
    for leak in ("correct_answer", '"answer"', "correct"):
        assert leak not in blob


async def test_practice_has_no_deadline(client, registered):
    s = await start(client, MODE_PRACTICE)
    assert s["expires_at"] is None
    assert s["max_errors"] is None


# --------------------------------------------------------------------------
# exam reveals nothing
# --------------------------------------------------------------------------

async def test_an_exam_answer_reveals_nothing(client, registered):
    """The absence IS the feature. Asserting on missing keys rather than on values,
    because the way this breaks is a field being added to the practice response and
    appearing here by accident."""
    s = await start(client)
    r = await client.post(
        f"/webapp/sessions/{s['id']}/answers", headers=auth(), json={"ordinal": 1, "answer": True}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    for leaked in ("correct", "correct_answer", "box", "explanation",
                   "explanation_state", "due_at", "wrong", "passed"):
        assert leaked not in body, f"exam answer leaked {leaked}"
    assert body["answered"] == 1
    assert body["remaining"] == s["question_count"] - 1


async def test_practice_does_reveal_the_verdict(client, registered):
    s = await start(client, MODE_PRACTICE)
    r = await client.post(
        f"/webapp/sessions/{s['id']}/answers", headers=auth(), json={"ordinal": 1, "answer": True}
    )
    assert r.status_code == 200, r.text
    assert "correct" in r.json()
    assert "correct_answer" in r.json()


async def test_results_are_refused_while_the_exam_is_open(client, registered):
    """This endpoint is what the whole no-feedback design exists to protect."""
    s = await start(client)
    r = await client.get(f"/webapp/sessions/{s['id']}/results", headers=auth())
    assert r.status_code == 409


async def test_results_are_available_once_submitted(client, registered):
    s = await start(client)
    await client.post(f"/webapp/sessions/{s['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": True})
    r = await client.post(f"/webapp/sessions/{s['id']}/finish", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answered"] == 1
    assert len(body["items"]) == body["question_count"]
    assert body["items"][0]["correct"] is not None


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

@pytest.mark.parametrize("verb,suffix", [
    ("get", ""), ("get", "/results"), ("post", "/finish"),
])
async def test_another_user_cannot_touch_your_session(client, registered, verb, suffix):
    """404 rather than 403: confirming an id exists is itself a leak, and session ids
    are small integers on a public surface."""
    s = await start(client)
    r = await getattr(client, verb)(
        f"/webapp/sessions/{s['id']}{suffix}", headers=auth(INTRUDER)
    )
    assert r.status_code == 404


async def test_another_user_cannot_answer_your_session(client, registered):
    s = await start(client)
    r = await client.post(
        f"/webapp/sessions/{s['id']}/answers",
        headers=auth(INTRUDER), json={"ordinal": 1, "answer": True},
    )
    assert r.status_code == 404


# --------------------------------------------------------------------------
# the two silent ones
# --------------------------------------------------------------------------

async def test_an_exam_does_not_spend_a_free_explanation_taster(client, registered, api_db):
    """record_answer used to call explanations.deliver unconditionally, and deliver is
    the ONE place free_explanations_used is incremented. Thirty answers would have burned
    all three tasters on text the mode never renders."""
    async with api_db() as s:
        before = (await s.get(User, OWNER)).free_explanations_used

    sess = await start(client)
    for ordinal in (1, 2, 3, 4):
        await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": True})

    async with api_db() as s:
        after = (await s.get(User, OWNER)).free_explanations_used
    assert after == before, "an exam spent a taster"


async def test_an_exam_records_no_paywall_hits(client, registered, api_db):
    """Up to 30 per sitting, for a paywall never shown, into an append-only table that
    cannot be cleaned - it would corrupt the one conversion metric §4.3 prices on."""
    sess = await start(client)
    for ordinal in (1, 2, 3):
        await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": True})
    async with api_db() as s:
        hits = (await s.scalars(select(Event).where(Event.type == EV_PAYWALL_HIT))).all()
    assert hits == []


async def test_an_exam_does_not_move_the_leitner_schedule(client, registered, api_db):
    """A pressured guess must not promote a question into the 7- or 30-day box, and 30
    re-stamped due_at values would dominate the practice queue afterwards. The corruption
    is silent and only visible weeks later."""
    sess = await start(client)
    qid = sess["questions"][0]["id"]

    # Answer CORRECTLY on purpose. A wrong answer leaves the box at 1 whether or not the
    # scheduler ran, so asserting box == 1 after an arbitrary answer is satisfiable by
    # accident - it passed even with scheduling deliberately switched back on. Only a
    # correct answer makes "promoted" and "not promoted" distinguishable.
    from api.models import Question

    async with api_db() as s:
        right = (await s.get(Question, qid)).answer

    await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": right})
    async with api_db() as s:
        progress = await s.get(Progress, (OWNER, qid))
    assert progress is not None, "the answer should still be counted"
    assert progress.seen == 1, "seen must still move - the user did see it"
    assert progress.box == 1, "box must NOT be promoted by a correct exam answer"


async def test_practice_does_move_the_schedule(client, registered, api_db):
    sess = await start(client, MODE_PRACTICE)
    qid = sess["questions"][0]["id"]
    correct = None
    async with api_db() as s:
        from api.models import Question
        correct = (await s.get(Question, qid)).answer
    await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": correct})
    async with api_db() as s:
        progress = await s.get(Progress, (OWNER, qid))
    assert progress.box == 2, "a correct practice answer should promote"


# --------------------------------------------------------------------------
# the clock belongs to the server
# --------------------------------------------------------------------------

async def test_an_expired_exam_is_graded_on_next_touch_using_its_own_deadline(
    client, registered, api_db
):
    """There is no scheduler, so expiry is noticed whenever the user comes back - which
    may be hours later because they closed Telegram. The grade must still use expires_at,
    not the moment of discovery."""
    sess = await start(client)
    async with api_db() as s:
        row = await s.get(QuizSession, sess["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await s.commit()

    r = await client.get(f"/webapp/sessions/{sess['id']}/results", headers=auth())
    assert r.status_code == 200, r.text
    assert r.json()["state"] == SESSION_EXPIRED
    assert r.json()["passed"] is False


async def test_answering_after_the_deadline_is_refused(client, registered, api_db):
    sess = await start(client)
    async with api_db() as s:
        row = await s.get(QuizSession, sess["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    r = await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": True})
    assert r.status_code == 409


async def test_starting_a_new_exam_grades_an_expired_one_rather_than_abandoning_it(
    client, registered, api_db
):
    """Order matters: sweeping abandoned sessions before enforcing deadlines would throw
    away a result the user actually earned while Telegram was backgrounded."""
    first = await start(client)
    async with api_db() as s:
        row = await s.get(QuizSession, first["id"])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await s.commit()

    await start(client)  # a fresh exam

    r = await client.get(f"/webapp/sessions/{first['id']}/results", headers=auth())
    assert r.json()["state"] == SESSION_EXPIRED, "the earlier exam should be graded, not abandoned"


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------

async def test_the_same_question_cannot_be_answered_twice(client, registered):
    """record_answer is not idempotent - a retry on a flaky mobile connection would
    double-count seen/wrong and re-run the scheduler."""
    sess = await start(client)
    a = await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": True})
    b = await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": False})
    assert a.status_code == 200
    assert b.status_code == 409


async def test_finish_is_idempotent(client, registered):
    """The client cannot tell a lost response from a rejected one."""
    sess = await start(client)
    first = await client.post(f"/webapp/sessions/{sess['id']}/finish", headers=auth())
    second = await client.post(f"/webapp/sessions/{sess['id']}/finish", headers=auth())
    assert first.status_code == second.status_code == 200
    assert first.json()["finished_at"] == second.json()["finished_at"]


async def test_an_unknown_ordinal_is_refused(client, registered):
    sess = await start(client)
    r = await client.post(f"/webapp/sessions/{sess['id']}/answers", headers=auth(),
                          json={"ordinal": 9999, "answer": True})
    assert r.status_code == 404


async def test_an_unknown_mode_is_refused(client, registered):
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": "cheating"})
    assert r.status_code == 422
