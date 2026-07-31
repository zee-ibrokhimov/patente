"""Changing your mind about an exam is not failing one.

`create` abandons any open sitting when a new one starts — of ANY mode, so tapping Practice
during an exam lands here. `_grade` then ran the pass/fail computation on it anyway, because
`max_errors` is not None, and `answered != question_count` made `passed = False`.

The profile filtered only on `state != SESSION_OPEN` and treated `passed is not None` as
"graded", so those rows counted in `taken`, in the pass rate, in `avg_errors` and in the
history list.

What the learner saw, reproduced by the audit: "Exams taken 2 · Passed 1 · Avg errors 0.0",
and a history entry reading FAILED 0/30 for a sitting they never took — a row that says they
got nothing wrong and still failed. The pass rate is pushed down while avg_errors is diluted
toward zero, so the two headline numbers on the readiness screen move in opposite wrong
directions. Nothing warned them that leaving an exam converts it into a failure.

EXPIRED IS DIFFERENT AND STAYS GRADED. Running out of time is a way to fail a real exam, not
a way to defer, and that is deliberate — see the comment in `_grade`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import QuizSession, User
from api.services import profile, quiz_sessions
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    MODE_EXAM,
    MODE_PRACTICE,
    SESSION_ABANDONED,
    SESSION_EXPIRED,
    SESSION_SUBMITTED,
)

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


async def _keys(api_db, paper) -> list[bool]:
    """The correct answers for a paper.

    An exam response deliberately carries no `answer` field — it reveals nothing until it
    is over, which is the invariant ExamAnswerOut exists to defend. So a test that needs to
    answer correctly has to look them up.
    """
    from api.models import Question

    async with api_db() as s:
        return [(await s.get(Question, q["id"])).answer for q in paper]


async def _sit_exam_then(client, api_db, follow_up_mode, *, answer=2):
    """Start an exam, answer `answer` questions correctly, then start `follow_up_mode` —
    which is what abandons the exam."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    keys = await _keys(api_db, started["questions"])
    for ordinal in range(1, answer + 1):
        await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": keys[ordinal - 1]})
    await client.post("/webapp/sessions", headers=auth(), json={"mode": follow_up_mode})
    async with api_db() as s:
        return await s.get(QuizSession, started["id"])


# --- the row itself ---------------------------------------------------------

async def test_an_abandoned_exam_is_not_graded(client, registered, api_db):
    row = await _sit_exam_then(client, api_db, MODE_PRACTICE)
    assert row.state == SESSION_ABANDONED
    assert row.passed is None, \
        "walking away from an exam was recorded as failing it"


async def test_tapping_practice_is_what_abandons_it(client, registered, api_db):
    """The path is not exotic. Any new sitting sweeps the open one, whatever its mode."""
    row = await _sit_exam_then(client, api_db, MODE_PRACTICE)
    assert row.state == SESSION_ABANDONED
    assert row.answered == 2


async def test_a_submitted_exam_is_still_graded(client, registered, api_db):
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())
    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
    assert row.state == SESSION_SUBMITTED
    assert row.passed is False, "an unfinished but SUBMITTED exam is a real fail"


async def test_an_expired_exam_is_still_graded(client, registered, api_db):
    """Running out of time is a way to fail a real exam, not a way to defer. The guard
    must not sweep this up with it."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
        await quiz_sessions.enforce_deadline(
            s, row, datetime.now(timezone.utc) + timedelta(hours=2))
        await s.commit()
        row = await s.get(QuizSession, started["id"])
    assert row.state == SESSION_EXPIRED
    assert row.passed is False


# --- what the profile says --------------------------------------------------

async def test_an_abandoned_exam_is_not_counted_as_taken(client, registered, api_db):
    """"Exams taken 2 · Passed 1" for someone who sat one."""
    first = (await client.post("/webapp/sessions", headers=auth(),
                               json={"mode": MODE_EXAM})).json()
    await client.post(f"/webapp/sessions/{first['id']}/finish", headers=auth())
    await _sit_exam_then(client, api_db, MODE_PRACTICE)

    body = (await client.get("/webapp/profile", headers=auth())).json()
    assert body["exams"]["taken"] == 1, \
        f"profile claims {body['exams']['taken']} exams taken, one was walked away from"


async def test_an_abandoned_exam_does_not_dilute_average_errors(client, registered, api_db):
    """It answered 0 wrong out of 2, so it dragged avg_errors toward zero while
    simultaneously counting as a failure — two headline numbers moving in opposite wrong
    directions from one row."""
    first = (await client.post("/webapp/sessions", headers=auth(),
                               json={"mode": MODE_EXAM})).json()
    keys = await _keys(api_db, first["questions"])
    for ordinal in (1, 2, 3):                      # three deliberate mistakes
        await client.post(f"/webapp/sessions/{first['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": not keys[ordinal - 1]})
    await client.post(f"/webapp/sessions/{first['id']}/finish", headers=auth())
    sat = (await client.get("/webapp/profile", headers=auth())).json()["exams"]["avg_errors"]
    assert sat == 3.0, "the sat exam should carry three errors"

    await _sit_exam_then(client, api_db, MODE_PRACTICE)
    after = (await client.get("/webapp/profile", headers=auth())).json()["exams"]["avg_errors"]

    assert after == sat, f"abandoning an exam changed avg_errors from {sat} to {after}"


async def test_an_abandoned_exam_is_not_in_the_history_list(client, registered, api_db):
    """The client renders a row as passed only when `passed` is exactly true, so an
    ungraded one would draw a red cross, a "failed" badge and a score of 0/30."""
    await _sit_exam_then(client, api_db, MODE_PRACTICE)
    body = (await client.get("/webapp/profile", headers=auth())).json()
    assert body["exams"]["recent"] == []


async def test_a_real_exam_still_appears_in_the_history(client, registered, api_db):
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())
    body = (await client.get("/webapp/profile", headers=auth())).json()
    assert [r["id"] for r in body["exams"]["recent"]] == [started["id"]]


async def test_every_history_row_can_be_rendered(client, registered, api_db):
    """The client reads `passed` as a tri-state it does not have a third branch for.
    Nothing reaching it may be ungraded."""
    first = (await client.post("/webapp/sessions", headers=auth(),
                               json={"mode": MODE_EXAM})).json()
    await client.post(f"/webapp/sessions/{first['id']}/finish", headers=auth())
    await _sit_exam_then(client, api_db, MODE_PRACTICE)

    body = (await client.get("/webapp/profile", headers=auth())).json()
    for row in body["exams"]["recent"]:
        assert row["passed"] is not None, f"history row {row['id']} has no verdict to draw"
