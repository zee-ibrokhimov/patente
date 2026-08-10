"""AI study advice, and the four rules that keep it from being a liability.

The breakdown screen says WHERE a learner is losing marks. This says what to do about it,
using their own recent mistakes. It is the first AI cost in this product that scales with
USERS rather than with content — explanations and translations are capped by the question
bank and shared by everybody, and the marginal cost of one more learner is zero. This one
is about one person and can never be reused, so the rules below are the only thing setting
its slope.

1. THE COOLDOWN IS CHECKED BEFORE ANYTHING ELSE. Checked after the entitlement or after the
   language, a learner who changes a setting gets a fresh analysis — four taps in Settings
   buy four of them inside one window, and one account can drive hundreds of calls an hour
   on a EUR 2.99 subscription.

2. IT NEVER BREAKS THE SCREEN. Every refusal and every failure is a 200 with a state. An AI
   layer that can take down the page it sits on is not worth the page.

3. NO NUMBERS IN THE PROSE. Every figure the learner sees is computed by the app. A model
   writing "your error rate is 31%" beside our computed 24% destroys trust in both, so the
   digits are stripped rather than merely forbidden — asking is not enforcing.

4. NOTHING IDENTIFYING IS SENT. No name, no Telegram id, no chat id.
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update as sa_update

from api.models import Analysis, Event, User
from api.services import coaching
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


@pytest.fixture
async def premium(client, registered, api_db):
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            pass_expires_at=datetime.now(timezone.utc) + timedelta(days=30)))
        await s.commit()
    return registered


@pytest.fixture
def model(monkeypatch):
    """A model that answers, and a record of what it was asked.

    Returns the captured request so a test can assert on what was SENT — which is half of
    what this feature has to get right.
    """
    sent: dict = {}

    class _Msg:
        content = json.dumps({
            "summary": "Confondi i segnali di divieto con quelli di obbligo.",
            "focus": [
                {"area": "Divieti", "action": "Rivedi i segnali tondi a bordo rosso."},
                {"area": "Obblighi", "action": "Confronta i tondi blu con i tondi rossi."},
                {"area": "Precedenza", "action": "Ripassa gli incroci senza segnaletica."},
            ],
            "habit": "Leggi il segnale prima della domanda.",
            "next_up": "Una sessione sui segnali di divieto.",
        })

    class _Choice:
        message = _Msg()

    class _Usage:
        prompt_tokens = 900
        completion_tokens = 300

    class _Resp:
        choices = [_Choice()]
        usage = _Usage()

    class _Completions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(coaching, "openai_client", lambda: _Client())
    return sent


async def wrong_answers(api_db, n: int, chat_id: int = OWNER) -> None:
    """`n` wrong answers, spread over distinct questions."""
    async with api_db() as s:
        from api.models import Question
        ids = list(await s.scalars(select(Question.id)))
        for i in range(n):
            s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                        payload={"question_id": ids[i % len(ids)], "correct": False}))
        await s.commit()


# --- it never breaks the screen ---------------------------------------------

async def test_a_refusal_is_a_state_and_not_an_error(client, registered, api_db):
    """A free learner with nothing behind them. 200 with a reason, so the breakdown
    underneath still renders."""
    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.status_code == 200
    assert r.json()["state"] in {"locked", "too_early"}


async def test_a_dead_model_is_a_state_too(client, premium, api_db, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "")
    await wrong_answers(api_db, 5)

    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.status_code == 200
    assert r.json()["state"] == "unavailable"


async def test_a_model_that_raises_does_not_reach_the_learner(
        client, premium, api_db, monkeypatch):
    class _Boom:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("upstream is having a bad afternoon")

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(coaching, "openai_client", lambda: _Boom())
    await wrong_answers(api_db, 5)

    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.status_code == 200
    assert r.json()["state"] == "unavailable"


# --- the cooldown -----------------------------------------------------------

async def test_a_second_ask_inside_the_window_costs_nothing(
        client, premium, api_db, model):
    await wrong_answers(api_db, 5)

    first = (await client.post("/webapp/analysis/coach", headers=auth())).json()
    assert first["state"] == "ready"

    calls_before = len(model)
    second = (await client.post("/webapp/analysis/coach", headers=auth())).json()
    assert second["state"] == "cooldown"
    assert second["summary"] == first["summary"], (
        "inside the cooldown the learner should re-read what they were given, not stare at "
        "a locked button"
    )
    assert second["available_at"] is not None

    async with api_db() as s:
        assert len(list(await s.scalars(select(Analysis)))) == 1, "a second call was billed"
    assert calls_before


async def test_changing_language_does_not_buy_a_second_analysis(
        client, premium, api_db, model):
    """The expensive bug this ordering exists to prevent. With the language checked first,
    four taps in Settings buy four analyses inside one window."""
    await wrong_answers(api_db, 5)
    assert (await client.post("/webapp/analysis/coach",
                              headers=auth())).json()["state"] == "ready"

    for lang in ("en", "uz", "it", "ru"):
        await client.patch("/webapp/settings", headers=auth(), json={"lang": lang})
        assert (await client.post("/webapp/analysis/coach",
                                  headers=auth())).json()["state"] == "cooldown"

    async with api_db() as s:
        assert len(list(await s.scalars(select(Analysis)))) == 1


def test_the_cooldown_is_checked_before_everything_else():
    """Stated against the source, because the ORDER is the rule. An entitlement check that
    runs first turns "has this account had one recently" into "is this account allowed one",
    and those are different questions."""
    src = (ROOT / "api" / "services" / "coaching.py").read_text(encoding="utf-8")
    body = src[src.index("async def may_generate("):]
    body = body[:body.index("\nasync def ")]
    assert body.index("COOLDOWN") < body.index("entitlement.premium"), (
        "the entitlement is checked before the cooldown"
    )


async def test_there_is_a_ceiling_under_the_cooldown(client, premium, api_db, monkeypatch):
    """A rate with no cap is still unbounded over a month."""
    monkeypatch.setattr(coaching, "MONTHLY_CAP", 2)
    monkeypatch.setattr(coaching, "COOLDOWN", timedelta(0))
    async with api_db() as s:
        for _ in range(2):
            s.add(Analysis(chat_id=OWNER, lang="ru", body={"summary": "x", "focus": []}))
        await s.commit()

    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.json()["state"] == "monthly_cap"


# --- what is sent -----------------------------------------------------------

async def test_nothing_identifying_leaves_the_building(client, premium, api_db, model):
    await wrong_answers(api_db, 5)
    await client.post("/webapp/analysis/coach", headers=auth())

    payload = json.dumps(model["messages"])
    assert str(OWNER) not in payload, "the chat id was sent to the model"
    for word in ("chat_id", "telegram", "user_id"):
        assert word not in payload.lower()


async def test_the_learners_own_mistakes_are_what_is_sent(client, premium, api_db, model):
    """The entire difference between specific advice and a horoscope."""
    await wrong_answers(api_db, 3)
    await client.post("/webapp/analysis/coach", headers=auth())

    payload = json.dumps(model["messages"], ensure_ascii=False)
    assert "recent_mistakes" in payload
    assert "Il segnale raffigurato vieta il transito" in payload


async def test_no_mistakes_means_no_call(client, premium, api_db, model):
    """Nothing to say, and no reason to pay for saying it."""
    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.json()["state"] == "unavailable"
    assert model == {}, "the model was called with nothing to analyse"


# --- what comes back --------------------------------------------------------

def test_numbers_are_stripped_from_the_prose():
    """Enforced, not requested. Every figure on that screen is computed by the app, and a
    model figure that disagrees with it destroys trust in both."""
    cleaned = coaching._clean({
        "summary": "Sbagli il 31% dei segnali di divieto e 12 domande su 30.",
        "focus": [{"area": "Divieti 2", "action": "Rivedi i 5 segnali principali."}],
        "habit": "Leggi 2 volte.",
        "next_up": "30 domande sui divieti.",
    })
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert not any(ch.isdigit() for ch in blob), blob
    assert "segnali di divieto" in cleaned["summary"], "the sentence should survive"


def test_an_empty_answer_is_rejected_rather_than_shown():
    assert coaching._clean({"summary": "", "focus": []}) is None
    assert coaching._clean({"summary": "ok", "focus": []}) is None


async def test_the_token_count_is_recorded(client, premium, api_db, model):
    """translations.py records a parameter being silently dropped by a retry for weeks, at
    5-10x the cost, with the constant set and the tests passing. The count on every row is
    what makes that visible in the data rather than in the invoice."""
    await wrong_answers(api_db, 5)
    await client.post("/webapp/analysis/coach", headers=auth())

    async with api_db() as s:
        row = (await s.scalars(select(Analysis))).one()
    assert row.tokens_in == 900 and row.tokens_out == 300


async def test_the_retry_ladder_degrades_one_parameter_at_a_time(
        client, premium, api_db, monkeypatch):
    """Copied from translations.py deliberately. A single fallback to NO parameters is how
    `reasoning_effort` came to be dropped on every call there."""
    tried: list[dict] = []

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    tried.append({k: kwargs[k] for k in ("temperature", "reasoning_effort")
                                  if k in kwargs})
                    if "temperature" in kwargs:
                        raise RuntimeError("does not support temperature with this model")
                    raise RuntimeError("nope")

    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    monkeypatch.setattr(coaching, "openai_client", lambda: _Client())
    await wrong_answers(api_db, 5)

    await client.post("/webapp/analysis/coach", headers=auth())
    assert tried[0] == {"temperature": 0, "reasoning_effort": coaching.REASONING_EFFORT}
    assert {"reasoning_effort": coaching.REASONING_EFFORT} in tried, (
        "the effort setting was never tried without temperature — which is exactly how it "
        "silently stopped being used in translations"
    )


# --- who may have one -------------------------------------------------------

async def test_a_free_learner_gets_one_taster_once_they_have_earned_it(
        client, registered, api_db, model, monkeypatch):
    monkeypatch.setattr(coaching, "TASTER_MIN_ANSWERS", 3)
    monkeypatch.setattr(coaching, "TASTER_MIN_AGE", timedelta(0))
    await wrong_answers(api_db, 3)

    assert (await client.post("/webapp/analysis/coach",
                              headers=auth())).json()["state"] == "ready"

    # And exactly one: the second is locked, not another taster.
    monkeypatch.setattr(coaching, "COOLDOWN", timedelta(0))
    assert (await client.post("/webapp/analysis/coach",
                              headers=auth())).json()["state"] == "locked"


async def test_a_fresh_account_cannot_harvest_a_taster(
        client, registered, api_db, model, monkeypatch):
    """The age condition. Without it, the taster is a reason to make accounts."""
    monkeypatch.setattr(coaching, "TASTER_MIN_ANSWERS", 3)
    monkeypatch.setattr(coaching, "TASTER_MIN_AGE", timedelta(days=7))
    await wrong_answers(api_db, 3)

    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.json()["state"] == "too_early"
    assert model == {}


def test_the_model_is_forbidden_from_teaching_traffic_law():
    """Rules are the explanations feature's job and those are written against the statute.
    An ungrounded model inventing a speed limit is the exact failure that system was built
    to prevent."""
    src = (ROOT / "api" / "services" / "coaching.py").read_text(encoding="utf-8")
    prompt = src[src.index("SYSTEM_PROMPT"):src.index('_DIGITS =')]
    assert "NEVER STATE A TRAFFIC RULE" in prompt
    assert "NO NUMBERS" in prompt


def test_spending_money_is_not_a_GET():
    """A GET that bills is a GET a browser, a prefetcher or a retry will fire on its own."""
    src = (ROOT / "api" / "routes" / "webapp.py").read_text(encoding="utf-8")
    assert '@router.post("/analysis/coach"' in src


async def test_an_old_account_with_few_answers_is_still_too_early(
        client, registered, api_db, model, monkeypatch):
    """The two taster conditions are independent, and the age one was masking the other:
    every test account is fresh, so removing the answer-count check changed nothing and
    mutation said so. An account can be a year old and still have nothing worth analysing.
    """
    monkeypatch.setattr(coaching, "TASTER_MIN_ANSWERS", 50)
    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == OWNER).values(
            created_at=datetime.now(timezone.utc) - timedelta(days=365)))
        await s.commit()
    await wrong_answers(api_db, 2)

    r = await client.post("/webapp/analysis/coach", headers=auth())
    assert r.json()["state"] == "too_early"
    assert model == {}, "the model was called for an account with nothing to say about"


# --- the client -------------------------------------------------------------

def _coach_block() -> str:
    src = (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    block = src[src.index("function coachBlock("):]
    return block[:block.index("\nasync function ")]


def test_the_advice_is_asked_for_and_never_loaded():
    """It may spend money. Fetching it on entering the screen would bill every learner who
    opened the breakdown, including the ones who only wanted the numbers."""
    src = (ROOT / "webapp" / "src" / "main.ts").read_text(encoding="utf-8")
    screen = src[src.index("function analysisScreen("):]
    screen = screen[:screen.index("\nfunction ")]
    assert "api.coach()" not in screen, "the screen fetches the advice on open"
    assert "coachBlock()" in screen
    assert "api.coach()" in src[src.index("async function askCoach("):][:400]


def test_every_refusal_has_something_to_say():
    """None of the states is an error, so each one needs words. A state with no branch
    renders an empty box under the numbers, which reads as a broken feature."""
    block = _coach_block()
    for state in ("locked", "too_early", "monthly_cap", "unavailable", "cooldown"):
        assert state in block, state
    i18n = (ROOT / "webapp" / "src" / "i18n.ts").read_text(encoding="utf-8")
    for key in ("coach_lead", "coach_ask", "coach_thinking", "coach_unavailable",
                "coach_locked", "coach_too_early", "coach_monthly_cap", "coach_cooldown",
                "coach_start"):
        assert i18n.count(f"{key}:") == 4, key


def test_the_previous_advice_is_shown_during_the_cooldown():
    """A learner inside the window should re-read what they were given rather than stare at
    a locked button — which is why the refusal carries the last body with it."""
    block = _coach_block()
    body = block[block.index("// ready, or cooldown"):]
    assert "c.summary" in body and "c.focus" in body


def test_the_advice_ends_in_something_to_do():
    """"advice a learner can't act on in one tap is advice they won't take"."""
    block = _coach_block()
    assert "coach_start" in block and "startRun(" in block
