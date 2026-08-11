"""How fast a learner may answer, and how many times a day.

`POST /webapp/answers` took any question id, any number of times, with no sitting, no pacing
rule and no limit — verified against the running app before this was written: two identical
posts of the same answer, back to back, both returned 200.

THAT ROUTE IS NOW DELETED. It also returned `correct_answer` for any id, so it was a free
exact oracle for the whole 7,106-question bank from the public internet, and it had no
caller. These tests drive the loopback twin `POST /users/{chat_id}/answers`, which is where
the write path lives — never published, because it takes its identity from the URL.

Harmless while nothing depended on the count. Not harmless now: the streak, the league
position and the free Premium days are all about to be derived from it, and every threshold
in those designs is a count of HTTP requests. A hundred answers takes about thirty seconds
to fake.

THE NUMBERS COME FROM THE REAL LOG, not from taste. The median gap between two answers by a
genuine learner is 7.4 seconds and roughly one genuine gap in 129 falls under 2.5 seconds; a
scripted run against the same database put 64 of 78 gaps under 2 seconds.

TWO RULES, AND ONLY ONE REFUSES ANYTHING. The first version of this refused a too-fast
answer, and sixteen existing tests failed at once — every one of them a sitting answered back
to back. That was the false-positive cost arriving as evidence rather than as an argument, so
the floor no longer refuses: it decides whether an answer is CREDITED, which is whether it
may count toward a streak, a league point or a free week. A fast answer is still recorded and
still moves the schedule, because the learner's study is real either way; it simply earns
nothing. The daily cap still refuses, because at 500 against a heaviest-genuine-day of 83 it
has no false positive worth the name.

The guard lives in `record_answer`, the single write path every mode shares, so a route
added later cannot skip it by forgetting to call it.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Event, Question
from api.services import pacing
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN, MODE_PRACTICE

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


@pytest.fixture(autouse=True)
def _no_floor_by_default(monkeypatch):
    """Most tests here are about OTHER rules and would trip the one-second floor simply by
    running fast. The floor gets its own test, which puts it back."""
    monkeypatch.setattr(pacing, "MIN_GAP", timedelta(0))


# --- the floor --------------------------------------------------------------

async def test_an_answer_in_the_same_instant_earns_nothing(
        client, registered, api_db, monkeypatch):
    """The loop this exists to stop, and the whole design in one test: the second answer is
    ACCEPTED and RECORDED, and marked as not counting."""
    monkeypatch.setattr(pacing, "MIN_GAP", timedelta(seconds=1))

    first = await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
    second = await client.post(f"/users/{OWNER}/answers", json={"question_id": 2, "answer": True})
    assert first.status_code == second.status_code == 200, (
        "refusing a real answer mid-exam is worse than letting a farmer through"
    )

    async with api_db() as s:
        credited = [json.loads(e.payload or "{}").get("credited") if isinstance(e.payload, str)
                    else (e.payload or {}).get("credited")
                    for e in await s.scalars(
                        select(Event).where(Event.type == EV_ANSWER_GIVEN)
                        .order_by(Event.id))]
    assert credited == [True, False], credited


async def test_a_fast_answer_still_counts_as_study(client, registered, api_db, monkeypatch):
    """It earns no reward; it is still the learner's own work. The schedule moves, the
    statistics move, and only the things that pay are withheld."""
    from api.models import Progress

    monkeypatch.setattr(pacing, "MIN_GAP", timedelta(seconds=1))
    await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
    await client.post(f"/users/{OWNER}/answers", json={"question_id": 2, "answer": True})

    async with api_db() as s:
        seen = len(list(await s.scalars(
            select(Progress).where(Progress.chat_id == OWNER))))
    assert seen == 2, "an uncredited answer was thrown away rather than merely not paying"


async def test_a_normal_pace_is_never_touched(client, registered, api_db):
    """One second is invisible to someone reading a question. If this ever fails, the floor
    has been raised to where it starts penalising real learners — which is worse than the
    farming it prevents.

    Asserts the CREDIT, not just the status code. Checking only that the request succeeded
    left "credit nothing, ever" indistinguishable from the real rule, because the very first
    answer of an account has no predecessor and is credited unconditionally — so a
    reward system that paid out on nothing would have looked identical here.
    """
    async with api_db() as s:
        ids = list(await s.scalars(select(Question.id)))
    for qid in ids:
        r = await client.post(f"/users/{OWNER}/answers", json={"question_id": qid, "answer": True})
        assert r.status_code == 200, f"a paced answer was refused: {r.text}"

    async with api_db() as s:
        rows = list(await s.scalars(
            select(Event).where(Event.type == EV_ANSWER_GIVEN).order_by(Event.id)))
    credited = [(json.loads(e.payload) if isinstance(e.payload, str) else (e.payload or {}))
                .get("credited") for e in rows]
    assert len(credited) == len(ids)
    assert all(credited), f"a properly paced answer earned nothing: {credited}"


# --- the daily cap ----------------------------------------------------------

async def test_the_day_has_a_ceiling(client, registered, api_db, monkeypatch):
    """Not there to pace a learner — the heaviest genuine day in the log is 83 answers — but
    to bound what a runaway can write."""
    monkeypatch.setattr(pacing, "DAILY_CAP", 3)

    for _ in range(3):
        r = await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
        assert r.status_code == 200

    r = await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
    assert r.status_code == 429


async def test_the_ceiling_rolls_rather_than_resetting_at_midnight(
        client, registered, api_db, monkeypatch):
    """A learner cut off at 23:59 must not be told to come back "tomorrow" sixty seconds
    later, and a rolling window has no timezone to get wrong."""
    monkeypatch.setattr(pacing, "DAILY_CAP", 2)
    for _ in range(2):
        await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})

    # Age the two answers past the window.
    async with api_db() as s:
        old = datetime.now(timezone.utc) - timedelta(days=1, minutes=5)
        for e in await s.scalars(select(Event).where(Event.type == EV_ANSWER_GIVEN)):
            e.created_at = old
        await s.commit()

    r = await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
    assert r.status_code == 200, "yesterday's answers are still holding the ceiling down"


# --- it covers every route --------------------------------------------------

async def test_the_guard_also_covers_answers_inside_a_sitting(
        client, registered, api_db, monkeypatch):
    """Two endpoints record answers. Guarding only the bare one would move the farm rather
    than close it — a practice sitting extends itself indefinitely."""
    monkeypatch.setattr(pacing, "DAILY_CAP", 1)

    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    first = await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                              json={"ordinal": 1, "answer": True})
    assert first.status_code == 200

    second = await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                               json={"ordinal": 2, "answer": True})
    assert second.status_code == 429


def test_the_guard_is_in_the_shared_write_path():
    """In `record_answer`, not in the routes. A route added later cannot skip a check it
    does not know exists — and there are already two routes that record answers."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "api" / "services" / "answers.py").read_text(encoding="utf-8")
    body = src[src.index("async def record_answer("):]
    body = body[:body.index("\nasync def ", 10)] if "\nasync def " in body[10:] else body
    assert "pacing.check(" in body
    # Before the first write, or a refused answer still moves the schedule.
    assert body.index("pacing.check(") < body.index("progress.seen"), (
        "the guard runs after progress has already been written"
    )


async def test_one_learners_pace_does_not_limit_another(client, registered, monkeypatch):
    """The window is per learner. A shared counter would let one script lock out everybody
    else — a denial of service dressed as a rate limit.

    Behavioural, because the first version of this asserted that a string appeared in the
    source, and that string appears in the OTHER query too: deleting the filter from the cap
    left the test passing. Mutation caught it.
    """
    monkeypatch.setattr(pacing, "DAILY_CAP", 2)

    for _ in range(2):
        r = await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})
        assert r.status_code == 200
    assert (await client.post(f"/users/{OWNER}/answers", json={"question_id": 1, "answer": True})).status_code == 429

    # A different learner, untouched by the first one's spending.
    #
    # The loopback route takes the chat id from the URL, so the second learner must be a
    # second URL. When these tests drove the signed Mini App route the distinction lived in
    # the header, and moving them over quietly collapsed both calls onto one account — the
    # test then asserted that a learner who had just been refused was not refused, and failed
    # honestly rather than passing as a stronger claim than it was making.
    await client.post("/users", json={"chat_id": 99_314, "lang": "ru"})
    other = await client.post("/users/99314/answers",
                              json={"question_id": 1, "answer": True})
    assert other.status_code == 200, (
        "one learner exhausting the cap locked out another"
    )


def test_the_public_answer_route_is_gone():
    """The oracle, asserted absent rather than merely unused.

    It accepted any question id with no sitting and returned `correct_answer`, so roughly
    2,000 requests bought the whole 7,106-question bank — and every threshold in the product
    (ten questions a day for the streak, a hundred answers for the analysis, twenty points to
    hold a rank) is a count that assumed those requests meant studying.

    Asserted on the route table, not by calling it: a 404 from a live server is also what a
    typo in a URL returns.
    """
    from api.routes import quiz, webapp

    # Against the routers, not `app.routes`: this app includes them lazily, so the
    # application object exposes six paths and none of the real ones. A test reading
    # `app.routes` would find "/webapp/answers" absent from an empty set and pass while the
    # route was fully alive.
    public = {r.path for r in webapp.router.routes}
    loopback = {r.path for r in quiz.router.routes}
    assert "/webapp/answers" not in public, "the answer-key oracle is reachable again"
    assert "/users/{chat_id}/answers" in loopback, "the loopback write path went with it"


def test_the_client_no_longer_offers_a_method_for_it():
    """A client method that 404s is an invitation to call it."""
    from pathlib import Path

    api_ts = (Path(__file__).resolve().parent.parent / "webapp/src/api.ts").read_text()
    assert 'request<AnswerResult>("/answers"' not in api_ts
