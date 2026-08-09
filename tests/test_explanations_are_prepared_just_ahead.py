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

This file existed because the audit finding was still true — warming WAS unreachable — and
the next person to notice would want to "fix" it.
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

async def test_starting_practice_generates_no_explanations_by_itself(
        client, registered, premium, generated):
    """Creating a sitting still prepares nothing. The preparation is a SEPARATE request the
    client makes behind its loading screen, so a session created by anything else — a
    resume, a test, a script — costs nothing."""
    r = await client.post("/webapp/sessions", headers=auth(), json={"mode": MODE_PRACTICE})
    assert r.status_code == 200, r.text
    assert generated == [], f"creating a sitting paid for {len(generated)} explanations"


async def test_preparing_a_window_does_warm_them(client, registered, premium, generated):
    """The reversal. Five questions ahead, on request, while the start screen is showing."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/prefetch", headers=auth(),
                          json={"from_ordinal": 1, "count": 5})
    assert r.status_code == 200, r.text
    assert r.json()["explanations"] >= 1, "the window prepared no explanations"


async def test_an_exam_prepares_no_explanations(client, registered, premium, generated):
    """MODE_OFFERS_EXPLANATION says an exam must not touch that path at all. Preparing
    thirty would be paid calls for text nobody is ever shown."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_EXAM})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/prefetch", headers=auth(),
                          json={"from_ordinal": 1, "count": 5})
    assert r.status_code == 200, r.text
    assert r.json()["explanations"] == 0


async def test_the_window_is_bounded(client, registered, premium):
    """A request for the whole paper would be the unbounded cost the first version had."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/prefetch", headers=auth(),
                          json={"from_ordinal": 1, "count": 500})
    assert r.status_code == 422


async def test_one_learner_cannot_prepare_anothers_sitting(client, registered, premium):
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    r = await client.post(f"/webapp/sessions/{started['id']}/prefetch", headers=auth(99),
                          json={"from_ordinal": 1, "count": 5})
    assert r.status_code == 404


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

def test_only_the_prefetch_route_warms_explanations():
    """The boundary that replaced "never warm". Preparation belongs to the one route the
    client calls behind its loading screen; anywhere else and it is unbounded again.

    Asserts WHERE the reference is, not how it is invoked. A previous version matched
    `add_task(\\s*explanations\\.warm` — the exact call form of the day — and broke the
    moment the route began awaiting its jobs instead of scheduling them, reporting "0 places
    warm explanations" for a change that moved nothing across the boundary. The rule is
    about location, so location is what is checked; comment mentions are stripped first,
    because the code here necessarily talks about `explanations.warm` in prose.
    """
    source = open(webapp_route.__file__, encoding="utf-8").read()
    code = "\n".join(
        "" if line.lstrip().startswith("#") else line.split("  #")[0]
        for line in source.splitlines()
    )

    hits = [i for i in range(len(code))
            if code.startswith("explanations.warm", i)]
    assert hits, "nothing warms explanations at all — the loading screen prepares nothing"

    # The span ends where the FUNCTION ends, not where the next route begins. Those are not
    # the same place: a module-level helper defined in the gap between them would sit inside
    # the looser span while being entirely outside the route, and a leak planted there
    # passed this test until the boundary was tightened. The end is therefore the next
    # top-level definition or decorator, whichever comes first.
    prefetch_at = code.index("async def prefetch(")
    ends = [code.index(marker, prefetch_at + 1)
            for marker in ("\n@router.", "\ndef ", "\nasync def ", "\nclass ")
            if marker in code[prefetch_at + 1:]]
    prefetch_end = min(ends)
    outside = [i for i in hits if not (prefetch_at < i < prefetch_end)]
    assert not outside, (
        f"{len(outside)} reference(s) to explanations.warm outside the prefetch route — "
        f"preparation is unbounded again")


def test_translations_are_still_warmed():
    """The distinction the decision rests on. Removing translation warming too would put a
    three-second wait in front of every single question, which is not the same trade."""
    source = open(webapp_route.__file__, encoding="utf-8").read()
    assert "translations.warm" in source


def test_the_loading_screen_makes_one_request_for_the_whole_window():
    """One call for five questions, not five calls for one.

    The first version of the waiting loading screen looped, issuing a prefetch per question
    so it could render "2 of 5". That is five round trips where the owner wants one, and the
    server already prepares the whole window inside a single request.

    Pinned because the loop is the tempting shape: it is what you write if you want a
    progress bar, and it reads perfectly well in review.
    """
    import pathlib
    main = (pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"
            / "main.ts").read_text(encoding="utf-8")
    start = main.index("const session = await sessions.start(mode, source)")
    end = main.index("enterRun(session)", start)
    block = main[start:end]

    calls = block.count("sessions.prefetch(")
    assert calls == 1, f"{calls} prefetch calls on the start path; the window is one request"
    assert "for (" not in block and "while (" not in block, \
        "the start path loops again — one request covers the whole window"
    assert ", true)" in block, "the loading screen no longer waits for the work"


def test_the_waited_prefetch_is_concurrency_bounded():
    """A five-question window queues up to ten model calls. Released together against a
    30,000 TPM account they do not go faster, they go 429.

    Checks the jobs pass THROUGH the gate, not merely that a gate is declared. The first
    version asserted `Semaphore(PREFETCH_CONCURRENCY)` was present and passed happily when
    the warms were switched back to launching around it — caught by mutation, not review.
    """
    source = open(webapp_route.__file__, encoding="utf-8").read()
    assert "_detach(bounded(" in source, "the warms are launched past the semaphore"
    assert "Semaphore(PREFETCH_CONCURRENCY)" in source, \
        "PREFETCH_CONCURRENCY is defined but nothing gates on it"


def test_the_loading_screen_waits_for_translations_but_not_explanations():
    """Different moments, different urgency.

    A translation is how the question is READ, so it must be there before the question
    appears. An explanation is wanted only after answering, if "Why?" is tapped — by which
    point it has had all the time the learner spent reading and deciding.

    Waiting on both made the screen unusable. Measured cold on five questions: ten jobs,
    three at a time, hit the 75-second deadline with FOUR unfinished, explanations at ~28s
    each being most of it. Reported from the outside as "in question 4 there was no
    translation and i waited again".
    """
    source = open(webapp_route.__file__, encoding="utf-8").read()
    start = source.index("if body.wait:")
    end = source.index("return {", start)
    block = source[start:end]

    assert 'if kind == "translation"' in block, \
        "the waited set is not restricted to translations"
    assert 'if kind != "translation"' in block, \
        "explanations are no longer started alongside — they would never be prepared"
    # The awaited list must be the filtered one, not every job.
    assert "asyncio.wait(blocking" in block, \
        "something other than the translation-only list is being awaited"


def test_translations_do_not_pay_for_reasoning():
    """Twelve seconds of thinking about a sentence with no ambiguity in it was most of the
    loading screen. Measured on gpt-5-mini: default 4.2-6.9s, low 2.8-4.2s, same text."""
    from api.services import translations

    assert translations.REASONING_EFFORT in ("low", "minimal"), \
        f"translations are back on full reasoning: {translations.REASONING_EFFORT}"
    source = open(translations.__file__, encoding="utf-8").read()
    assert "reasoning_effort=REASONING_EFFORT" in source, \
        "the constant is set but never passed to the model"
