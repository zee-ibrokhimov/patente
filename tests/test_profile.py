"""The profile screen's numbers.

Readiness is a claim made to someone about to sit a real exam, so the tests that matter
most are the ones about when it refuses to answer.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from api.models import Event, Progress, QuizSession
from api.services import profile
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN, MODE_EXAM, SESSION_SUBMITTED

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


async def answered(api_db, question_id: int, seen: int, wrong: int, when: datetime):
    async with api_db() as s:
        s.add(Progress(chat_id=OWNER, question_id=question_id, box=1, due_at=when,
                       seen=seen, wrong=wrong, last_answer_at=when))
        await s.commit()


# --- readiness refuses to guess -------------------------------------------

async def test_readiness_is_null_below_the_minimum_sample(client, registered, api_db):
    """Four correct answers is not "100% ready". Printing that would be a lie told at
    exactly the moment the product is asking to be trusted."""
    await answered(api_db, 1, seen=4, wrong=0, when=datetime.now(timezone.utc))
    r = await client.get("/webapp/profile", headers=auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["readiness"] is None
    assert body["readiness_sample"] == 4
    assert body["readiness_min_sample"] == profile.MIN_SAMPLE


async def test_readiness_appears_once_there_is_enough_evidence(client, registered, api_db):
    now = datetime.now(timezone.utc)
    await answered(api_db, 1, seen=100, wrong=20, when=now)
    r = await client.get("/webapp/profile", headers=auth())
    assert r.json()["readiness"] == pytest.approx(0.8)


async def test_twenty_answers_is_not_evidence(client, registered, api_db):
    """MIN_SAMPLE was 20 — 0.28% of a 7106-question bank. One good evening, or one lucky
    run on a topic someone happens to know already.

    Putting a percentage on a gauge after that, about a legally required exam that costs
    money and a re-sit to fail, is a claim the data cannot support. A learner told "84%
    ready" on twenty answers who then fails has been misled by the thing they revised
    with. 100 is roughly three mock exams' worth.
    """
    await answered(api_db, 1, seen=20, wrong=2, when=datetime.now(timezone.utc))
    body = (await client.get("/webapp/profile", headers=auth())).json()
    assert body["readiness"] is None, "20 answers must not produce a readiness figure"
    assert body["readiness_sample"] == 20
    assert body["readiness_min_sample"] >= 100


async def test_the_pass_bar_is_reported_so_the_number_means_something(client, registered):
    """27 of 30. Without it, "80% ready" is a percentage of nothing."""
    r = await client.get("/webapp/profile", headers=auth())
    assert r.json()["pass_accuracy"] == pytest.approx(0.9)


# --- streak ----------------------------------------------------------------

async def _answer_events(api_db, days_ago: list[int]):
    async with api_db() as s:
        for d in days_ago:
            s.add(Event(chat_id=OWNER, type=EV_ANSWER_GIVEN,
                        created_at=datetime.now(timezone.utc) - timedelta(days=d)))
        await s.commit()


async def test_a_streak_counts_consecutive_days(client, registered, api_db):
    await _answer_events(api_db, [0, 1, 2])
    assert (await client.get("/webapp/profile", headers=auth())).json()["streak_days"] == 3


async def test_a_streak_survives_not_having_studied_yet_today(client, registered, api_db):
    """Studied last night, opens the app this morning before studying. Breaking the
    streak at a midnight the server chose punishes the user for the server's timezone."""
    await _answer_events(api_db, [1, 2, 3])
    assert (await client.get("/webapp/profile", headers=auth())).json()["streak_days"] == 3


async def test_a_gap_ends_the_streak(client, registered, api_db):
    await _answer_events(api_db, [0, 1, 4, 5])
    assert (await client.get("/webapp/profile", headers=auth())).json()["streak_days"] == 2


async def test_an_old_streak_does_not_count(client, registered, api_db):
    await _answer_events(api_db, [5, 6, 7])
    assert (await client.get("/webapp/profile", headers=auth())).json()["streak_days"] == 0


async def test_no_activity_is_zero_not_an_error(client, registered):
    body = (await client.get("/webapp/profile", headers=auth())).json()
    assert body["streak_days"] == 0
    assert body["readiness"] is None
    assert body["exams"]["taken"] == 0


# --- exam history ----------------------------------------------------------

async def test_exam_history_counts_only_finished_sittings(client, registered, api_db):
    """An exam in progress must not appear in history — that would leak its running
    error count, which is the one thing exam mode exists to hide."""
    now = datetime.now(timezone.utc)
    async with api_db() as s:
        s.add(QuizSession(chat_id=OWNER, mode=MODE_EXAM, state=SESSION_SUBMITTED,
                          started_at=now, finished_at=now, question_count=30,
                          max_errors=3, answered=30, wrong=2, passed=True))
        s.add(QuizSession(chat_id=OWNER, mode=MODE_EXAM, state=SESSION_SUBMITTED,
                          started_at=now, finished_at=now, question_count=30,
                          max_errors=3, answered=30, wrong=7, passed=False))
        s.add(QuizSession(chat_id=OWNER, mode=MODE_EXAM, state="open",
                          started_at=now, question_count=30, max_errors=3,
                          answered=5, wrong=4))
        await s.commit()

    exams = (await client.get("/webapp/profile", headers=auth())).json()["exams"]
    assert exams["taken"] == 2, "the open exam must not be counted"
    assert exams["passed"] == 1
    assert exams["avg_errors"] == pytest.approx(4.5)
    assert all(e["state"] != "open" for e in exams["recent"])


async def test_another_users_exams_are_not_visible(client, registered, api_db):
    """The other user has to exist: quiz_sessions.chat_id is a real FK onto users, which
    is what makes /delete a single cascading delete rather than an orphan sweep."""
    now = datetime.now(timezone.utc)
    await client.post("/users", json={"chat_id": 999999, "lang": "en"})
    async with api_db() as s:
        s.add(QuizSession(chat_id=999999, mode=MODE_EXAM, state=SESSION_SUBMITTED,
                          started_at=now, finished_at=now, question_count=30,
                          max_errors=3, answered=30, wrong=0, passed=True))
        await s.commit()
    assert (await client.get("/webapp/profile", headers=auth())).json()["exams"]["taken"] == 0


async def test_profile_requires_a_signature(client):
    assert (await client.get("/webapp/profile")).status_code == 401
