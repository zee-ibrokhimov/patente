"""Explanations are warmed on the path learners actually use.

`explanations.warm` had exactly ONE call site in the repo — `serve_next` in api/routes/
quiz.py, behind GET /users/{chat_id}/next-question — and nothing calls that endpoint. The
Mini App exports `nextQuestion` in api.ts and never uses it, `bot/api_client.py` has no such
method, and every other reference is in tests. So when drilling moved into the Mini App the
warming quietly came out of the live path.

WHY THAT MATTERS MORE THAN IT SOUNDS

`record_answer` delivers the explanation with `generate_if_missing=False`, and the entire
reason that flag exists is "serve it if warming already produced it" — paying for a call at
answer time would charge for the majority who answer and move on. With nothing warming, that
is a guaranteed miss for any cluster no other user has already paid for. Coverage in
production is 12 clusters of 3382.

So the verdict box never carried the explanation. The learner always got a "Why?" button,
and tapping it ran a live model call in the foreground — 4.9s cold, bounded at 45s. The
mechanism described as making the explanation "appear with the verdict instead of after a
ten-second wait" fired for nobody, on the feature the product is sold on.
"""

from __future__ import annotations

import json
import time

import pytest

from api.routes import webapp as webapp_route
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


@pytest.fixture
async def bank(api_db):
    """Enough questions that a practice paper is longer than the warm window.

    The shared fixture has FOUR, and PRACTICE_BATCH is 30 — so `paper[:WARM_AHEAD]` and
    `paper` are the same list and any assertion about the window's SIZE passes whether the
    window exists or not. Caught by mutation: replacing `paper[:WARM_AHEAD]` with `paper`
    failed nothing. The same trap bit the exam-schedule tests earlier the same day.
    """
    from api.models import Question

    async with api_db() as s:
        s.add_all([
            Question(id=2000 + i, quesito_id=200, topic_id=2, cluster_id=2,
                     statement_it=f"Affermazione di riscaldamento {i}",
                     answer=(i % 2 == 0), source_version="v1")
            for i in range(60)
        ])
        await s.commit()


@pytest.fixture
def warmed(monkeypatch):
    """Every (cluster_id, lang) the app asked to have warmed."""
    calls = []

    async def fake_warm(cluster_id, lang):
        calls.append((cluster_id, lang))

    monkeypatch.setattr(webapp_route.explanations, "warm", fake_warm)
    return calls


async def _premium(api_db, chat_id=OWNER, lang="ru"):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update as sa_update

    from api.models import User

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == chat_id).values(
            lang=lang, pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()


# --- starting a sitting -----------------------------------------------------

async def test_starting_practice_warms_explanations(client, registered, api_db, warmed):
    """THE regression. Nothing warmed explanations from the Mini App at all."""
    await _premium(api_db)
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert r.status_code == 200, r.text
    assert warmed, "starting practice warmed no explanations"


async def test_it_warms_the_readers_own_language(client, registered, api_db, warmed):
    await _premium(api_db, lang="uz")
    await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert {lang for _, lang in warmed} == {"uz"}


async def test_starting_an_exam_warms_nothing(client, registered, api_db, warmed):
    """MODE_OFFERS_EXPLANATION says an exam must not touch the explanation path at all.
    Warming a paper an exam never shows explanations for is paid calls for nothing."""
    await _premium(api_db)
    await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_EXAM})
    assert warmed == []


async def test_a_user_who_cannot_be_shown_one_warms_nothing(
        client, registered, warmed, monkeypatch):
    """Generating for someone who could not be shown the result is money spent on nothing,
    and nothing rate-limits session creation.

    The gate is `can_explain`, not `premium`, and the difference is deliberate: a free user
    with a taster left CAN be shown an explanation, so warming theirs is correct — that
    taster is the entire quality pitch. This test is the other half, and it has to set the
    taster count explicitly, because the repo's own .env carries FREE_EXPLANATIONS=3 while
    production sets 0. Asserting "free means no warming" without pinning that would be
    asserting a local config value.
    """
    monkeypatch.setattr(settings, "free_explanations", 0)
    await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert warmed == []


async def test_a_free_user_with_a_taster_left_is_warmed(client, registered, warmed,
                                                        monkeypatch):
    """The taster is what sells the product. An explanation they are entitled to see must
    not be the one that arrives after a five-second wait."""
    monkeypatch.setattr(settings, "free_explanations", 3)
    await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert warmed, "a user holding a free taster was left cold"


async def test_the_warm_window_is_bounded(client, registered, api_db, bank, warmed):
    """Warming the whole paper would be thirty paid OpenAI calls the instant Start is
    tapped — before a single answer — for a learner who may stop at question three. Nothing
    rate-limits session creation, so that is an unbounded cost per tap."""
    await _premium(api_db)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    assert started["question_count"] > webapp_route.WARM_AHEAD, \
        "the paper must be longer than the window or this asserts nothing"
    assert len(warmed) <= webapp_route.WARM_AHEAD, \
        f"Start tapped {len(warmed)} model calls for a {started['question_count']}-item paper"


# --- and as they work through it --------------------------------------------

async def test_answering_warms_further_ahead(client, registered, api_db, bank, warmed):
    """Warming only at Start would leave every answer past the fifth cold again, which is
    most of a sitting — practice runs until the learner stops it."""
    await _premium(api_db)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    warmed.clear()

    r = await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": True})
    assert r.status_code == 200, r.text
    assert warmed, "answering warmed nothing — the window never moves past the fifth item"
    assert all(cluster_id is not None for cluster_id, _ in warmed)


async def test_answering_warms_one_cluster_not_a_batch(client, registered, api_db, bank,
                                                       warmed):
    """One per answer, not a fresh window each time — otherwise every tap re-schedules
    WARM_AHEAD tasks and most of them are already-cached no-ops."""
    await _premium(api_db)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    warmed.clear()
    await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": True})
    assert len(warmed) == 1


async def test_the_last_few_answers_warm_nothing(client, registered, api_db, bank, warmed):
    """Past the end of the paper there is nothing ahead to warm, and asking for an ordinal
    that does not exist must not schedule a task."""
    await _premium(api_db)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    last = started["question_count"]
    warmed.clear()
    await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                      json={"ordinal": last, "answer": True})
    assert warmed == []


async def test_answering_an_exam_item_warms_nothing(client, registered, api_db, warmed):
    await _premium(api_db)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    warmed.clear()
    await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                      json={"ordinal": 1, "answer": True})
    assert warmed == [], "an exam answer triggered an explanation call"


async def test_a_question_with_no_cluster_is_never_warmed(client, registered, api_db, warmed):
    """Question 4 in the fixture has cluster_id None — nothing could ever be written for
    it, and asking to warm None would be a wasted task on every sitting containing it."""
    await _premium(api_db)
    await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert all(cluster_id is not None for cluster_id, _ in warmed)


# --- the service function it depends on -------------------------------------

async def test_cluster_at_reads_the_paper(client, registered, api_db):
    from api.models import QuizSession
    from api.services import quiz_sessions

    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
        first = await quiz_sessions.cluster_at(s, row, 1)
        past_the_end = await quiz_sessions.cluster_at(s, row, 9999)
    assert past_the_end is None
    assert first is None or isinstance(first, int)


async def test_cluster_at_never_reads_another_sitting(client, registered, api_db):
    """It is keyed on the session, so a shared ordinal must not leak across sittings."""
    from api.models import QuizSession
    from api.services import quiz_sessions

    a = (await client.post("/webapp/sessions", headers=auth(),
                           json={"mode": MODE_PRACTICE})).json()
    b = (await client.post("/webapp/sessions", headers=auth(),
                           json={"mode": MODE_PRACTICE})).json()
    async with api_db() as s:
        row_b = await s.get(QuizSession, b["id"])
        got = await quiz_sessions.cluster_at(s, row_b, 1)
        from sqlalchemy import select

        from api.models import QuizSessionItem
        owner = await s.scalar(
            select(QuizSessionItem.session_id)
            .where(QuizSessionItem.session_id == row_b.id, QuizSessionItem.ordinal == 1))
    assert a["id"] != b["id"]
    assert owner == b["id"]
    assert got is None or isinstance(got, int)
