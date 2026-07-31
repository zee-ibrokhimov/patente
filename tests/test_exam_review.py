"""After an exam you can see which questions you got wrong.

The largest gap in the product. A candidate finished a sitting, was told "11 errors /
3 allowed", and that was the end of it — no way to see WHICH eleven, no way to learn
anything from having sat it. The results endpoint returned ordinals and verdicts and no
question text, so the client could not have shown them even if it wanted to.

The moment a learner most wants the material is the moment they have just failed. It was
the one moment the app gave them nothing.

The line these tests defend: the QUESTION and the CORRECT ANSWER are free — the whole
bank is in the free tier already, and hiding a ministerial statement behind a paywall
would be hiding public content. The EXPLANATION is not, and must stay behind the gate it
has always had.
"""

from __future__ import annotations

import json
import time

import pytest

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


async def sat_exam(client, answers: list[bool] | None = None):
    """Start a sitting, answer some of it, submit."""
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": "practice"})
    session = r.json()
    for i, given in enumerate(answers or [True, False], start=1):
        await client.post(f"/webapp/sessions/{session['id']}/answers", headers=auth(),
                          json={"ordinal": i, "answer": given})
    done = await client.post(f"/webapp/sessions/{session['id']}/finish", headers=auth())
    return done.json()


# --- the questions come back ------------------------------------------------

async def test_results_carry_the_question_text(client, registered):
    """Without this the client can say "you got 11 wrong" and nothing else."""
    body = await sat_exam(client)
    assert body["items"], "no items at all"
    assert any(i["statement"] for i in body["items"]), \
        "results carry no question text — a review screen is impossible"


async def test_results_carry_the_correct_answer(client, registered):
    """Seeing which you got wrong is half of it; seeing what was right is the other."""
    body = await sat_exam(client)
    answered = [i for i in body["items"] if i["given"] is not None]
    assert answered
    assert all(i["answer"] is not None for i in answered)


async def test_the_verdict_is_consistent_with_the_answer(client, registered):
    """`correct` and (`given` == `answer`) must agree, or the review screen would show a
    green tick above two contradicting values."""
    body = await sat_exam(client)
    for i in body["items"]:
        if i["given"] is None:
            continue
        assert i["correct"] == (i["given"] == i["answer"]), f"item {i['ordinal']} disagrees"


async def test_an_unanswered_question_is_distinguishable_from_a_wrong_one(client, registered):
    """In an exam a blank counts against you, so it belongs in the mistakes list — but it
    is not the same thing as answering incorrectly and must not read as one."""
    body = await sat_exam(client, answers=[True])
    blanks = [i for i in body["items"] if i["given"] is None]
    assert blanks, "expected unanswered items"
    assert all(i["correct"] is None for i in blanks)


async def test_the_figure_comes_back_so_a_sign_question_can_be_reviewed(client, registered):
    """Half the bank is about a pictured sign. Reviewing one without the picture is
    reviewing nothing."""
    body = await sat_exam(client)
    assert "image" in body["items"][0]


# --- the paywall line -------------------------------------------------------

async def test_a_free_user_can_still_review_their_mistakes(client, registered, api_db):
    """The question and the answer are ministerial content and the free tier already
    includes the whole bank. Charging to see what you got wrong would be charging for
    something the app gives away one screen earlier."""
    from tests.conftest import end_trial

    await end_trial(api_db, OWNER)
    body = await sat_exam(client)
    assert any(i["statement"] for i in body["items"])
    assert any(i["answer"] is not None for i in body["items"] if i["given"] is not None)


async def test_a_free_user_gets_no_translation_in_the_review(client, registered, api_db):
    """Translations are Premium. The review screen must not become a way to read them
    without paying — it returns thirty of them at once."""
    from tests.conftest import end_trial

    await end_trial(api_db, OWNER)
    body = await sat_exam(client)
    assert all(i["translation"] is None for i in body["items"])


async def test_translations_are_withheld_when_the_user_turned_them_off(client, registered):
    """Entitlement is not the only gate: a paying user who switched translations off
    should not get them back through a different screen."""
    await client.patch("/webapp/settings", headers=auth(), json={"translations_on": False})
    body = await sat_exam(client)
    assert all(i["translation"] is None for i in body["items"])


async def test_the_review_does_not_include_explanations(client, registered):
    """The reasoning is the paid product. The review lists what happened and offers a
    button; it never ships the explanation in the results payload, where thirty of them
    would be handed out at once."""
    body = await sat_exam(client)
    raw = json.dumps(body)
    assert "explanation" not in raw


async def test_results_are_still_refused_while_the_sitting_is_open(client, registered):
    """The whole no-feedback design. Carrying the questions in the results must not open
    a way to read the answer key mid-exam."""
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": "exam"})
    sid = r.json()["id"]
    assert (await client.get(f"/webapp/sessions/{sid}/results", headers=auth())).status_code == 409


async def test_someone_elses_results_stay_theirs(client, registered):
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": "practice"})
    sid = r.json()["id"]
    await client.post(f"/webapp/sessions/{sid}/finish", headers=auth())
    other = await client.get(f"/webapp/sessions/{sid}/results", headers=auth(99))
    assert other.status_code in (403, 404)
