"""A resumed exam knows which questions are already answered.

`GET /webapp/sessions/{id}` returned `answered` as a COUNT and the frozen paper — nothing
per-item. The client keeps no state across a reopen (`answered: new Set()` on every
`enterRun`, and there is no localStorage anywhere in webapp/src), and `results()` — the only
function that returns per-ordinal data — refuses an open sitting with 409. So there was
literally no way for the client to learn which ordinals it had done.

What that looked like: answer 20 of 30, take a phone call, reopen, tap Resume. You land on
question 21 with the correct remaining time, above a thirty-circle answer sheet with every
circle blank. The header says 21 of 30; the sheet says nothing has been done. The sheet's
own comment in main.ts says its purpose is "showing which you have done".

NOTHING WAS EVER LOST — and that is what made it dangerous rather than merely wrong. The
server refuses a second answer for the same ordinal with a 409 before any counter moves, and
Submit still returns all thirty. But for the remaining minutes of a timed test the learner
cannot know that, and the obvious reaction is to start over — which abandons the sitting.

ORDINALS ONLY. Returning `given` or `correct` would put a hole in "an exam reveals nothing
until it is over", which ExamAnswerOut and its tests exist to defend. Knowing you answered
question 7 is not knowing whether you got it right.
"""

from __future__ import annotations

import json
import time

import pytest

from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import MODE_EXAM, MODE_PRACTICE

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


async def _start(client, mode=MODE_EXAM):
    return (await client.post("/webapp/sessions", headers=auth(),
                              json={"mode": mode})).json()


async def _answer(client, session_id, ordinal, given=True):
    return await client.post(f"/webapp/sessions/{session_id}/answers", headers=auth(),
                             json={"ordinal": ordinal, "answer": given})


# --- the sheet ---------------------------------------------------------------

async def test_a_resumed_sitting_reports_which_ordinals_are_done(client, registered):
    """THE bug: the client had no way to know, so it painted every circle blank."""
    started = await _start(client)
    await _answer(client, started["id"], 1)
    await _answer(client, started["id"], 2)

    resumed = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    assert resumed["answered_ordinals"] == [1, 2]


async def test_an_untouched_sitting_reports_none(client, registered):
    started = await _start(client)
    resumed = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    assert resumed["answered_ordinals"] == []


async def test_it_matches_the_count_the_header_uses(client, registered):
    """The header reads from `answered` and the sheet from this. Two sources that can
    disagree is what produced "Question 3 of 4" above an empty sheet."""
    started = await _start(client)
    for ordinal in (1, 2, 3):
        await _answer(client, started["id"], ordinal)

    resumed = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    assert len(resumed["answered_ordinals"]) == resumed["answered"]


async def test_practice_gets_it_too(client, registered):
    started = await _start(client, MODE_PRACTICE)
    await _answer(client, started["id"], 1)
    resumed = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    assert resumed["answered_ordinals"] == [1]


async def test_a_fresh_session_response_carries_the_field(client, registered):
    """`enterRun` reads it on every entry, not only on resume, so it must always be
    present rather than only on the resume route."""
    started = await _start(client)
    assert started["answered_ordinals"] == []


# --- and reveals nothing else ------------------------------------------------

async def test_the_resume_payload_still_hides_the_answers(client, registered):
    """The invariant this had to be built around. An exam reveals nothing until it is
    over — so the sheet learns WHICH, never WHAT."""
    started = await _start(client)
    await _answer(client, started["id"], 1)

    body = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    blob = json.dumps(body)
    assert "correct" not in blob
    assert "given" not in blob
    for question in body["questions"]:
        assert "answer" not in question


async def test_one_user_cannot_read_anothers_sheet(client, registered):
    started = await _start(client)
    await _answer(client, started["id"], 1)
    r = await client.get(f"/webapp/sessions/{started['id']}", headers=auth(99))
    assert r.status_code == 404


# --- the 409 that had no way out ---------------------------------------------

async def test_answering_the_same_ordinal_twice_is_still_refused(client, registered):
    """The server-side guarantee the client now leans on: a second answer changes nothing
    and says so, rather than double-counting."""
    started = await _start(client)
    first = await _answer(client, started["id"], 1)
    assert first.status_code == 200

    again = await _answer(client, started["id"], 1)
    assert again.status_code == 409
    assert "already answered" in again.json()["detail"]

    body = (await client.get(f"/webapp/sessions/{started['id']}", headers=auth())).json()
    assert body["answered"] == 1


async def test_the_refusal_message_is_what_the_client_matches_on(client, registered):
    """webapp/src/main.ts treats a 409 whose detail contains "already answered" as
    success — the answer IS recorded — and advances instead of showing a dead-end error.
    Changing this string silently reintroduces the trap, so it is pinned here."""
    started = await _start(client)
    await _answer(client, started["id"], 1)
    again = await _answer(client, started["id"], 1)
    assert again.json()["detail"] == "already answered"
