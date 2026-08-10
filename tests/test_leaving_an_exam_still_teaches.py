"""Exit: leave a timed exam without sitting it, and still see how you did.

Two requirements pull in opposite directions and both have to hold.

**It must not count.** An exam nobody sat is not an exam they failed. The sitting is closed
as ABANDONED, which `_grade` deliberately leaves ungraded (`passed = None`), and the profile
counts only rows where `passed` is not None. See test_abandoned_exams_are_not_failures.py for
the bug that established that rule — a history row reading FAILED 0/30 for a sitting the
learner never took.

**It must still teach.** The moment a learner stops is the moment they most want to know what
they got wrong, and the old behaviour — leave, land on the home screen, learn nothing — threw
that away. So exit returns the same review payload as submitting, and the client shows the
questions they answered with verdicts and explanations.

The distinction from the pre-existing implicit abandon (starting any new sitting sweeps the
open one) is that this one is deliberate, is reachable from a control, and hands back the
review instead of silently discarding the sitting.

WHAT EXIT DOES NOT UNDO: the answers. Twelve answered questions are twelve real pieces of
evidence about what someone knows, and readiness is accuracy over recent ANSWERS rather than
over exams. Discarding them would make a learner who exits look less practised than they are
and would punish the honest use of a control we added on purpose. That is a judgement call
and `test_the_answers_given_still_count_as_practice` pins it so it stays a decision rather
than drifting.
"""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import select

from api.models import Event, Progress, Question, QuizSession, QuizSessionItem
from api.services import profile
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    EV_ANSWER_GIVEN,
    MODE_EXAM,
    SESSION_ABANDONED,
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


async def _keys(api_db, session_id: int) -> dict[int, bool]:
    """ordinal -> correct answer, read from the PAPER rather than from the response.

    The session response carries a window of questions, not all thirty — an exam answering
    test that indexes into it silently runs out after the first page. The paper is in the
    database from the moment the sitting is created, so read it there.
    """
    async with api_db() as s:
        rows = (await s.execute(
            select(QuizSessionItem.ordinal, Question.answer)
            .join(Question, Question.id == QuizSessionItem.question_id)
            .where(QuizSessionItem.session_id == session_id)
        )).all()
    return {ordinal: answer for ordinal, answer in rows}


async def sit(client, api_db, *, answer: int | None = None, wrong: int = 0):
    """Start an exam and answer `answer` questions, `wrong` of them incorrectly.

    `answer=None` means the whole paper. The seeded bank is four questions, so the sizes
    here are small on purpose — what matters is the ratio of answered to total, not thirty.
    """
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    keys = await _keys(api_db, started["id"])
    if answer is None:
        answer = started["question_count"]
    assert answer <= started["question_count"], "the paper is smaller than the test assumes"
    for ordinal in range(1, answer + 1):
        key = keys[ordinal]
        given = (not key) if ordinal <= wrong else key
        await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": given})
    return started


# --- it does not count ------------------------------------------------------

async def test_exiting_closes_the_sitting_without_grading_it(client, registered, api_db):
    started = await sit(client, api_db, answer=2)
    r = await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())
    assert r.status_code == 200

    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
    assert row.state == SESSION_ABANDONED
    assert row.passed is None, "exiting an exam was recorded as failing it"
    assert row.finished_at is not None, "the sitting must not be left open"


async def test_an_exited_exam_is_not_an_exam_taken(client, registered, api_db):
    """The headline number on the readiness screen. A learner who exits three exams and
    sits one must read "1", not "4"."""
    for _ in range(3):
        started = await sit(client, api_db, answer=1)
        await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())

    sat = await sit(client, api_db, answer=None)
    await client.post(f"/webapp/sessions/{sat['id']}/finish", headers=auth())

    async with api_db() as s:
        exams = (await profile.user_profile(s, OWNER))["exams"]
    assert exams["taken"] == 1, exams
    assert exams["passed"] == 1, exams
    assert len(exams["recent"]) == 1, "an uncounted sitting appeared in the history list"


async def test_exiting_does_not_dilute_the_average(client, registered, api_db):
    """An exited sitting has few errors simply because it has few answers. Averaged in, it
    drags avg_errors toward zero — the flattering direction, which is the worst one for a
    number a learner uses to decide whether to book the real exam."""
    sat = await sit(client, api_db, answer=None, wrong=2)
    await client.post(f"/webapp/sessions/{sat['id']}/finish", headers=auth())
    async with api_db() as s:
        honest = (await profile.user_profile(s, OWNER))["exams"]["avg_errors"]

    walked = await sit(client, api_db, answer=2)
    await client.post(f"/webapp/sessions/{walked['id']}/exit", headers=auth())

    async with api_db() as s:
        after = (await profile.user_profile(s, OWNER))["exams"]["avg_errors"]
    assert after == honest, f"avg_errors moved from {honest} to {after} on an exit"


# --- it still teaches -------------------------------------------------------

async def test_exiting_hands_back_the_review(client, registered, api_db):
    """The whole second half of the request. Leaving used to land on the home screen with
    nothing learned."""
    started = await sit(client, api_db, answer=3, wrong=2)
    body = (await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())).json()

    assert body["state"] == SESSION_ABANDONED
    assert body["passed"] is None, "the client renders null as 'not counted'; false is a fail"
    assert body["answered"] == 3
    assert body["wrong"] == 2
    assert len(body["items"]) == started["question_count"]


async def test_the_review_says_which_answered_questions_were_wrong(client, registered, api_db):
    """"show questions where user give answer with correct or wrong" — so each answered item
    has to carry a verdict, and the question text with it. A list of ordinals would tell a
    learner they got three wrong and nothing they could learn from."""
    started = await sit(client, api_db, answer=3, wrong=2)
    body = (await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())).json()

    answered = [i for i in body["items"] if i["given"] is not None]
    assert len(answered) == 3
    assert sum(1 for i in answered if i["correct"] is False) == 2
    assert all(i["statement"] for i in answered), "a verdict with no question teaches nothing"
    assert all(i["answer"] is not None for i in answered), "the correct answer must be shown"


async def test_the_untouched_questions_are_distinguishable_from_mistakes(client, registered, api_db):
    """Eighteen questions never reached are not eighteen mistakes.

    In a SUBMITTED exam a blank does count against you, and the review deliberately files it
    with the errors. On an exit it must not: the client filters on `given`, so the field has
    to be null for anything untouched rather than defaulted to something falsy.
    """
    started = await sit(client, api_db, answer=2)
    body = (await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())).json()

    untouched = [i for i in body["items"] if i["given"] is None]
    assert len(untouched) == started["question_count"] - 2
    assert all(i["correct"] is None for i in untouched), \
        "an unanswered question came back with a verdict, and would be shown as a mistake"


async def test_exits_cannot_push_real_exams_out_of_the_record(client, registered, api_db):
    """The history window is twenty rows. Its filter used to run in PYTHON, over the twenty
    rows the query had already returned — so ungraded sittings occupied slots and evicted
    real exams from the window, and `taken`, `passed` and `avg_errors` are all computed from
    that same twenty. Twenty-one exits and a learner's entire exam record reads as zero.

    Nearly unreachable while abandoning required the detour of starting another sitting.
    A button made it a thing someone could do in a minute, which is why the filter is now
    in SQL and why this exists.
    """
    sat = await sit(client, api_db, answer=None, wrong=1)
    await client.post(f"/webapp/sessions/{sat['id']}/finish", headers=auth())

    for _ in range(22):
        walked = await sit(client, api_db, answer=1)
        await client.post(f"/webapp/sessions/{walked['id']}/exit", headers=auth())

    async with api_db() as s:
        exams = (await profile.user_profile(s, OWNER))["exams"]
    assert exams["taken"] == 1, f"the real exam was evicted by exits: {exams}"
    assert len(exams["recent"]) == 1, exams["recent"]
    assert exams["avg_errors"] == 1.0, exams


async def test_an_answer_records_which_sitting_it_came_from(client, registered, api_db):
    """Readiness counts answers given inside an exited sitting, on purpose. That decision can
    only ever be revisited if the events can tell those answers apart — and `events` is
    append-only, so a discriminator not written at the time cannot be recovered later."""
    started = await sit(client, api_db, answer=2)
    await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())

    async with api_db() as s:
        payloads = [e.payload for e in await s.scalars(
            select(Event).where(Event.type == EV_ANSWER_GIVEN))]
    assert payloads, "no answer events were written"
    assert all(p.get("session_id") == started["id"] for p in payloads), payloads


# --- the judgement call, pinned ---------------------------------------------

async def test_the_answers_given_still_count_as_practice(client, registered, api_db):
    """Exiting drops the EXAM, not the learning.

    Deliberate, and the opposite choice is defensible — so it is pinned here rather than
    left to be rediscovered. The answers were real; readiness is accuracy over recent
    answers; a learner who uses the exit honestly should not appear to have practised less
    than one who force-quits the app.
    """
    started = await sit(client, api_db, answer=3)
    await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())

    async with api_db() as s:
        seen = len(list(await s.scalars(
            select(Progress).where(Progress.chat_id == OWNER))))
    assert seen == 3, "the answers given before exiting were discarded"


# --- shape --------------------------------------------------------------------

async def test_exiting_twice_is_not_an_error(client, registered, api_db):
    """The client cannot tell a lost response from a refused one, and a retried exit that
    500s would strand a learner on a screen with no way out — which is the bug this whole
    feature exists to fix."""
    started = await sit(client, api_db, answer=2)
    first = await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())
    second = await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())
    assert first.status_code == second.status_code == 200
    assert second.json()["state"] == SESSION_ABANDONED


async def test_a_submitted_exam_cannot_be_downgraded_by_exiting(client, registered, api_db):
    """Otherwise a failed exam could be erased from the record by tapping Exit afterwards."""
    started = await sit(client, api_db, answer=None, wrong=4)
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())
    await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth())

    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
    assert row.state == SESSION_SUBMITTED
    assert row.passed is False


async def test_you_cannot_exit_someone_elses_exam(client, registered, api_db):
    started = await sit(client, api_db, answer=2)
    r = await client.post(f"/webapp/sessions/{started['id']}/exit", headers=auth(9999))
    assert r.status_code in (403, 404)

    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
    assert row.state != SESSION_ABANDONED
