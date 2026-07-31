"""Repeating what you got wrong, and what you got right.

Asked for directly by the owner: "can repeat correct answered questions and also uncorrect
ones? cuz this is important".

`practice_paper` already resurfaces mistakes, but on the Leitner schedule — when the
algorithm decides you are about to forget, mixed in with new material, and never on demand.
That is the right default and the wrong tool for "my test is Friday, show me everything I
have ever got wrong". Revision to a deadline wants a deliberate pass over a known set.

WHAT IS AND IS NOT DIFFERENT

Only the DRAW. A repeat round grades answers and moves the Leitner schedule exactly like any
other practice, because the learner is genuinely studying — the alternative would be a mode
where working through your mistakes teaches the app nothing.

WRONG means `wrong > 0`: ever missed, not currently-missed. A question missed twice and got
right once is still one they have struggled with, and dropping it the moment it goes right
would make the mode quietly forget the material it exists to drill.

RIGHT means seen and never wrong — deliberately strict, so anything with a mistake against
it appears in exactly one of the two lists.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Progress, Question, User
from api.services import selection
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    MODE_EXAM,
    MODE_PRACTICE,
    REPEAT_CORRECT,
    REPEAT_SMART,
    REPEAT_WRONG,
)

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


@pytest.fixture
async def bank(api_db):
    """Sixty questions, so a 30-item paper is a real selection rather than the whole pool.

    The shared fixture has four. Two earlier test files in this suite silently asserted
    nothing because a 30-question draw from four questions returns the same rows whatever
    the query says.
    """
    async with api_db() as s:
        s.add_all([
            Question(id=3000 + i, quesito_id=200, topic_id=2, cluster_id=2,
                     statement_it=f"Affermazione di ripasso {i}",
                     answer=(i % 2 == 0), source_version="v1")
            for i in range(60)
        ])
        await s.commit()
        return [q for q in (await s.scalars(
            select(Question.id).where(Question.id >= 3000).order_by(Question.id))).all()]


async def _history(api_db, *, wrong: list[int], right: list[int], when=None):
    """Give the learner a past: `wrong` answered incorrectly, `right` always correctly."""
    when = when or NOW - timedelta(days=1)
    async with api_db() as s:
        for i, qid in enumerate(wrong):
            s.add(Progress(chat_id=OWNER, question_id=qid, box=1, due_at=NOW,
                           seen=2, wrong=1, last_answer_at=when + timedelta(seconds=i)))
        for i, qid in enumerate(right):
            s.add(Progress(chat_id=OWNER, question_id=qid, box=3, due_at=NOW + timedelta(days=2),
                           seen=1, wrong=0, last_answer_at=when + timedelta(seconds=i)))
        await s.commit()


async def _user(api_db):
    async with api_db() as s:
        return await s.get(User, OWNER)


# --- the draw ----------------------------------------------------------------

async def test_the_wrong_round_serves_only_mistakes(api_db, registered, bank):
    await _history(api_db, wrong=bank[:8], right=bank[8:20])
    async with api_db() as s:
        picked = await selection.repeat_paper(s, await s.get(User, OWNER), REPEAT_WRONG, 30)
    assert {q.id for q in picked} == set(bank[:8])


async def test_the_correct_round_serves_only_questions_never_missed(api_db, registered, bank):
    await _history(api_db, wrong=bank[:8], right=bank[8:20])
    async with api_db() as s:
        picked = await selection.repeat_paper(s, await s.get(User, OWNER), REPEAT_CORRECT, 30)
    assert {q.id for q in picked} == set(bank[8:20])


async def test_the_two_lists_never_overlap(api_db, registered, bank):
    """Strict on purpose: a question with a mistake against it belongs in one list, so a
    learner reviewing "what I know" is not shown something they have got wrong."""
    await _history(api_db, wrong=bank[:8], right=bank[8:20])
    async with api_db() as s:
        user = await s.get(User, OWNER)
        w = {q.id for q in await selection.repeat_paper(s, user, REPEAT_WRONG, 30)}
        c = {q.id for q in await selection.repeat_paper(s, user, REPEAT_CORRECT, 30)}
    assert w & c == set()


async def test_a_question_got_wrong_then_right_stays_in_the_wrong_list(
        api_db, registered, bank):
    """`wrong > 0` is EVER missed, not currently-missed. Dropping it the moment it goes
    right would make the mode forget the material it exists to drill — and one correct
    answer is not evidence a rule is learned."""
    async with api_db() as s:
        s.add(Progress(chat_id=OWNER, question_id=bank[0], box=4,
                       due_at=NOW + timedelta(days=7),
                       seen=5, wrong=2, last_answer_at=NOW))
        await s.commit()
    async with api_db() as s:
        picked = await selection.repeat_paper(s, await s.get(User, OWNER), REPEAT_WRONG, 30)
    assert [q.id for q in picked] == [bank[0]]


async def test_unseen_questions_are_in_neither(api_db, registered, bank):
    """A repeat round repeats. New material is what the default mode is for."""
    await _history(api_db, wrong=bank[:2], right=bank[2:4])
    async with api_db() as s:
        user = await s.get(User, OWNER)
        w = {q.id for q in await selection.repeat_paper(s, user, REPEAT_WRONG, 30)}
        c = {q.id for q in await selection.repeat_paper(s, user, REPEAT_CORRECT, 30)}
    assert not (w | c) & set(bank[4:])


async def test_the_longest_untouched_comes_first(api_db, registered, bank):
    """Oldest `last_answer_at` first: what you have not looked at for longest is likeliest
    to have gone stale, and repeated rounds then walk the set instead of re-serving the
    same head. Asserting POSITION, not membership — membership passes by luck too often."""
    async with api_db() as s:
        for i, qid in enumerate(bank[:5]):
            s.add(Progress(chat_id=OWNER, question_id=qid, box=1, due_at=NOW, seen=1,
                           wrong=1, last_answer_at=NOW - timedelta(days=10 - i)))
        await s.commit()
    async with api_db() as s:
        picked = await selection.repeat_paper(s, await s.get(User, OWNER), REPEAT_WRONG, 30)
    assert [q.id for q in picked] == bank[:5]


async def test_one_users_history_never_reaches_another(api_db, registered, bank):
    async with api_db() as s:
        s.add(User(chat_id=99, lang="ru"))
        await s.commit()
    await _history(api_db, wrong=bank[:5], right=[])
    async with api_db() as s:
        other = await s.get(User, 99)
        assert await selection.repeat_paper(s, other, REPEAT_WRONG, 30) == []


# --- through the API ---------------------------------------------------------

async def test_practice_can_be_started_as_a_repeat_round(client, registered, api_db, bank):
    await _history(api_db, wrong=bank[:6], right=bank[6:12])
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": MODE_PRACTICE, "source": REPEAT_WRONG})
    assert r.status_code == 200, r.text
    assert {q["id"] for q in r.json()["questions"]} == set(bank[:6])


async def test_the_default_is_unchanged(client, registered, api_db, bank):
    """Existing clients send no `source`. They must keep the spaced-repetition draw."""
    await _history(api_db, wrong=bank[:6], right=bank[6:12])
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert r.status_code == 200
    assert r.json()["question_count"] > 6, \
        "the default practice draw was narrowed to the repeat set"


async def test_an_exam_ignores_the_source(client, registered, api_db, bank):
    """A simulator drawn from your own mistakes reports a score that means nothing."""
    await _history(api_db, wrong=bank[:3], right=[])
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": MODE_EXAM, "source": REPEAT_WRONG})
    assert r.status_code == 200
    assert r.json()["question_count"] > 3


async def test_an_unknown_source_is_refused(client, registered):
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": MODE_PRACTICE, "source": "everything"})
    assert r.status_code == 422


async def test_nothing_to_repeat_is_its_own_answer(client, registered, bank):
    """A learner who has never got one wrong asking for their mistakes is a normal,
    explainable state — and needs a different answer from "the question bank is missing",
    which is a 503 about the server rather than about them."""
    r = await client.post("/webapp/sessions", headers=auth(),
                          json={"mode": MODE_PRACTICE, "source": REPEAT_WRONG})
    assert r.status_code == 409
    assert "repeat" in r.json()["detail"]


# --- a repeat round is still practice ----------------------------------------

async def test_answers_in_a_repeat_round_still_move_the_schedule(
        client, registered, api_db, bank):
    """Only the DRAW changes. A mode where working through your mistakes taught the app
    nothing would be a worse product than not having it."""
    await _history(api_db, wrong=bank[:4], right=[])
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE,
                                       "source": REPEAT_WRONG})).json()
    first = started["questions"][0]["id"]
    async with api_db() as s:
        before = (await s.get(Progress, (OWNER, first))).due_at

    async with api_db() as s:
        answer = (await s.get(Question, first)).answer
    await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": answer})

    async with api_db() as s:
        after = await s.get(Progress, (OWNER, first))
    assert after.due_at != before, "a repeat answer did not reschedule the question"
    assert after.seen == 3


async def test_a_repeat_round_reveals_the_verdict_like_practice(
        client, registered, api_db, bank):
    await _history(api_db, wrong=bank[:4], right=[])
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE,
                                       "source": REPEAT_WRONG})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": True})
    assert "correct" in r.json(), "a repeat round hid the verdict like an exam"
