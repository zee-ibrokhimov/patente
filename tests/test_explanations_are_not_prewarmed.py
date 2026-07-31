"""Explanations are NOT warmed. This is a decision about money, not an oversight.

An audit found that `explanations.warm` had become unreachable — its only call site was
`serve_next` in api/routes/quiz.py, behind an endpoint nothing calls — and warming was added
to the Mini App's session routes to fix it. The owner reversed that the same day, on cost,
and was right to.

THE ARITHMETIC

  · A TRANSLATION is ~150 tokens in, ~300 out, and every non-Italian learner needs one on
    every question. Warming ahead is cheap and always used. It stays.
  · An EXPLANATION is ~8000 tokens in, because the statute goes in the prompt, and only some
    fraction of learners ever tap "Why?". Warming the next five questions buys a faster
    explanation for the few who ask by paying for it for everyone who does not — and it is
    unbounded per Start tap, since nothing rate-limits session creation.

On a product with a handful of users and no revenue yet, that is the wrong trade. An
explanation is generated when someone asks for it and cached per CLUSTER for everyone
afterwards, so the cost is a one-time ~5s wait for the first person to ask about that rule,
and the saving is never paying for the 3370 clusters nobody reaches.

This file exists because the audit finding is still true — warming IS unreachable — and the
next person to notice will want to "fix" it. It is not a bug. Revisit only when traffic makes
the cache fill itself.
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
def generated(monkeypatch):
    """Every cluster the app paid a model call for, by any route."""
    from api.services import explanations

    calls = []

    async def fake_generate(session, cluster_id, model=None):
        calls.append(cluster_id)
        return explanations.Outcome("declined")

    async def fake_warm(cluster_id, lang):
        calls.append(cluster_id)

    monkeypatch.setattr(explanations, "generate", fake_generate)
    monkeypatch.setattr(explanations, "warm", fake_warm)
    return calls


@pytest.fixture
async def premium(api_db):
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import update as sa_update

    from api.models import User

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()


# --- starting and working through a sitting costs nothing -------------------

async def test_starting_practice_generates_no_explanations(
        client, registered, premium, generated):
    """THE cost decision. Thirty questions must not become a batch of paid calls the
    instant Start is tapped."""
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert r.status_code == 200, r.text
    assert generated == [], f"starting practice paid for {len(generated)} explanations"


async def test_starting_an_exam_generates_no_explanations(
        client, registered, premium, generated):
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_EXAM})
    assert r.status_code == 200, r.text
    assert generated == []


async def test_answering_generates_no_explanation(client, registered, premium, generated):
    """`record_answer` delivers with `generate_if_missing=False` — it reports whether an
    explanation CAN be offered, and never produces one. That flag is the whole mechanism
    and it must not be relaxed."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": 1, "answer": True})
    assert r.status_code == 200, r.text
    assert generated == [], "answering a practice question paid for an explanation"


async def test_a_whole_practice_run_costs_nothing(client, registered, premium, generated):
    """The case that matters: someone drills for twenty minutes without ever asking why."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    for ordinal in range(1, started["question_count"] + 1):
        await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": True})
    assert generated == [], \
        f"a full practice run cost {len(generated)} model calls with nobody asking why"


# --- asking DOES pay, once, for everyone ------------------------------------

async def test_tapping_why_is_what_pays(client, registered, premium, generated, api_db):
    """The other half. Refusing to warm is only defensible because the explicit request
    still works — that is where the ~5s goes, and only for the learner who asked.

    The cached rows are cleared first. The fixture ships question 1's cluster with an
    approved Russian explanation, so without this the request is a cache hit and the test
    would pass while proving nothing — which is what it did when first written.
    """
    from sqlalchemy import delete

    from api.models import Explanation

    async with api_db() as s:
        await s.execute(delete(Explanation))
        await s.commit()

    r = await client.post("/webapp/questions/1/explanation", headers=auth())
    assert r.status_code == 200, r.text
    assert generated, "asking for an explanation generated nothing"


async def test_the_result_is_cached_for_everyone_after_the_first_ask(
        client, registered, premium, api_db):
    """Cached per CLUSTER, so the first person to ask about a rule pays for every learner
    who meets any question in it. That is what makes on-demand affordable."""
    from sqlalchemy import select

    from api.models import Explanation

    async with api_db() as s:
        rows = (await s.scalars(
            select(Explanation).where(Explanation.cluster_id == 1))).all()
    assert rows, "the fixture's cluster 1 should already hold a cached explanation"
    langs = {r.lang for r in rows}
    assert "ru" in langs


# --- the wiring that would silently reintroduce the cost --------------------

def test_the_session_routes_do_not_call_warm():
    """Pinned on the source, because this is a decision rather than a behaviour — and the
    audit finding that prompted the original change is still true, so somebody will read it
    and want to help.

    Matches the CALL, not the name: the comment explaining the decision necessarily mentions
    `explanations.warm`, and the first version of this test failed on its own explanation.
    """
    import re

    source = open(webapp_route.__file__, encoding="utf-8").read()
    live = re.findall(r"^\s*(?!#).*add_task\(\s*explanations\.warm", source, re.M)
    assert live == [], \
        "explanation warming is back in the Mini App routes — see this module's docstring"


def test_the_reason_is_written_down_next_to_the_code():
    """A bare absence reads as an oversight and gets 'fixed'. The comment is the guard."""
    source = open(webapp_route.__file__, encoding="utf-8").read()
    assert "DELIBERATELY" in source and "8000 tokens" in source


def test_translations_are_still_warmed():
    """The distinction the decision rests on. Removing translation warming too would put a
    three-second wait in front of every single question, which is not the same trade."""
    source = open(webapp_route.__file__, encoding="utf-8").read()
    assert "translations.warm" in source
