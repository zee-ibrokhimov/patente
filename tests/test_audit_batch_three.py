"""Three audit findings: a lost exam, an unhearable complaint, and a silent language swap.

Each was a whole path that existed, worked, was tested — and could not be reached from the
screen a learner actually uses. They share a cause: things were built while the bot was
the quiz, and when drilling moved into the Mini App the wiring was not carried across.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Report, User
from api.services.telegram_auth import sign
from shared.config import settings

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


# --- 1. a sitting you walked away from --------------------------------------

async def test_me_reports_an_open_sitting(client, registered):
    """The client could only know about a sitting it had watched the user leave in the
    SAME page load. Close the Mini App mid-exam — a phone call, the screen locking — and
    it was gone, while the twenty minutes kept running on the server."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": "exam"})).json()
    me = (await client.get("/webapp/me", headers=auth())).json()
    assert me["open_session_id"] == started["id"]


async def test_a_finished_sitting_is_not_offered_back(client, registered):
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": "practice"})).json()
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())
    me = (await client.get("/webapp/me", headers=auth())).json()
    assert me["open_session_id"] is None


async def test_no_sitting_means_nothing_to_recover(client, registered):
    assert (await client.get("/webapp/me", headers=auth())).json()["open_session_id"] is None


async def test_the_newest_sitting_is_the_one_offered(client, registered):
    """Starting a second sitting abandons the first, so only the most recent open row can
    still be returned to. Offering the older one would resume a graded exam."""
    first = (await client.post("/webapp/sessions", headers=auth(),
                               json={"mode": "practice"})).json()
    second = (await client.post("/webapp/sessions", headers=auth(),
                                json={"mode": "practice"})).json()
    me = (await client.get("/webapp/me", headers=auth())).json()
    assert me["open_session_id"] == second["id"]
    assert me["open_session_id"] != first["id"]


async def test_one_user_is_never_offered_anothers_sitting(client, registered):
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": "exam"})).json()
    other = (await client.get("/webapp/me", headers=auth(99))).json()
    assert other["open_session_id"] != started["id"]
    assert other["open_session_id"] is None


# --- 2. "this explanation is wrong" -----------------------------------------

async def test_the_mini_app_can_report_a_bad_explanation(client, registered, api_db):
    """The endpoint existed, was tested, and lived only on the loopback route the BOT
    used. Once drilling moved into the Mini App nobody could report anything, and the
    owner could not hear about a single wrong explanation — while report volume per
    thousand served is the quality metric for a feature sold entirely on quality."""
    r = await client.post("/webapp/reports", headers=auth(), json={"question_id": 1})
    assert r.status_code == 201
    async with api_db() as s:
        rows = (await s.scalars(select(Report).where(Report.chat_id == OWNER))).all()
    assert len(rows) == 1
    assert rows[0].question_id == 1


async def test_reporting_an_unknown_question_is_refused(client, registered):
    r = await client.post("/webapp/reports", headers=auth(), json={"question_id": 99999})
    assert r.status_code == 404


async def test_reporting_requires_a_signature(client, registered):
    assert (await client.post("/webapp/reports", json={"question_id": 1})).status_code == 401


async def test_a_report_records_the_reporters_language(client, registered, api_db):
    """A wrong explanation is wrong in ONE language. Without this the owner would have to
    guess which of three to re-read."""
    from sqlalchemy import update as sa_update

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(lang="uz"))
        await s.commit()
    await client.post("/webapp/reports", headers=auth(), json={"question_id": 1})
    async with api_db() as s:
        row = (await s.scalars(select(Report).where(Report.chat_id == OWNER))).one()
    assert row.lang == "uz"


# --- 3. the silent language swap --------------------------------------------

async def test_an_explanation_states_its_language(client, registered, api_db):
    """Uzbek falls back to Russian on purpose — a bad explanation is the only thing on
    screen and is the thing being sold, so Uzbek ships as translations first. But nothing
    told the reader: they paid for explanations and silently got a language they had not
    chosen. Saying so is the difference between a known limit and a broken product."""
    from sqlalchemy import update as sa_update

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(lang="uz"))
        await s.commit()

    r = await client.post("/webapp/questions/1/explanation", headers=auth())
    body = r.json()
    if body["explanation_state"] == "shown":
        assert body["explanation_lang"] == "ru", "an Uzbek reader is served Russian"


async def test_a_russian_reader_is_told_russian(client, registered):
    r = await client.post("/webapp/questions/1/explanation", headers=auth())
    body = r.json()
    if body["explanation_state"] == "shown":
        assert body["explanation_lang"] == "ru"


def test_the_answer_schema_carries_the_language_too():
    """Practice delivers the explanation WITH the verdict, through a different schema.
    Adding the field to only one of them would leave the note missing on the screen where
    explanations are actually read."""
    from api.schemas import AnswerOut, ExplanationOut

    assert "explanation_lang" in AnswerOut.model_fields
    assert "explanation_lang" in ExplanationOut.model_fields
