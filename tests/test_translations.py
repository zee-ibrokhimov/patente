"""On-demand question translation.

The translation is a comprehension aid under the Italian, never a replacement — the
candidate is learning to recognise the exact ministerial phrasing. So what these defend
is mostly *restraint*: don't paraphrase the schema, don't invent a sign the model cannot
see, don't overwrite something a human reviewed, and above all don't put a translation
call in front of the question.

No real API call is made — `openai_client` is substituted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import end_trial
from sqlalchemy import select

from api.models import Question, Translation, User
from api.services import translations
from api.services.entitlement import Access, Entitlement
from api.services.explanations import openai_client  # noqa: F401  (patched by name below)

REPLY = {
    "ru": {"stem": None, "statement": "Изображённый знак запрещает движение"},
    "en": {"stem": None, "statement": "The sign shown prohibits transit"},
}


class FakeClient:
    def __init__(self, reply=None, error=None):
        self.reply = REPLY if reply is None else reply
        self.error = error
        self.calls = 0
        self.messages = None
        self.model = None
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls += 1
        self.messages = kwargs.get("messages")
        self.model = kwargs.get("model")
        await asyncio.sleep(0.01)
        if self.error:
            raise self.error
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {
                "content": json.dumps(self.reply, ensure_ascii=False)})()})()],
            "usage": type("U", (), {"prompt_tokens": 40, "completion_tokens": 30})(),
        })()


@pytest.fixture
def fake_openai(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(translations, "openai_client", lambda: client)
    return client


@pytest.fixture
async def untranslated(api_db):
    """api_db ships question 1 with an RU translation already; clear it."""
    async with api_db() as s:
        for row in (await s.scalars(select(Translation))).all():
            await s.delete(row)
        await s.commit()


def entitled():
    return Entitlement(has_pass=True, pass_expires_at=None, free_explanations_left=0)


def reader(lang="ru", on=True):
    return type("U", (), {"chat_id": 42, "lang": lang, "translations_on": on})()


# --- generating --------------------------------------------------------------

async def test_one_call_stores_both_languages(api_db, fake_openai, untranslated):
    async with api_db() as s:
        question = await s.get(Question, 1)
        assert await translations.generate(s, question) is True
        stored = {t.lang: t.statement for t in (await s.scalars(
            select(Translation).where(Translation.question_id == 1)
        )).all()}
    assert set(stored) == {"ru", "en"}
    assert stored["en"] == "The sign shown prohibits transit"
    assert fake_openai.calls == 1


async def test_a_second_reader_costs_nothing(api_db, fake_openai, untranslated):
    async with api_db() as s:
        question = await s.get(Question, 1)
        await translations.ensure(s, question, "ru")
        await translations.ensure(s, question, "ru")
        await translations.ensure(s, question, "en")   # cached by the same call
    assert fake_openai.calls == 1


async def test_the_prompt_carries_the_statement_and_no_image(api_db, fake_openai, untranslated):
    """Translating does not need the figure, and asking for it would invite the model to
    name the sign — which is a wrong answer printed directly under the question."""
    async with api_db() as s:
        question = await s.get(Question, 1)
        await translations.generate(s, question)
    sent = json.loads(fake_openai.messages[-1]["content"])
    assert sent["statement"] == "Il segnale raffigurato vieta il transito"
    assert all(isinstance(m["content"], str) for m in fake_openai.messages)


async def test_a_reviewed_translation_is_never_overwritten(api_db, fake_openai):
    async with api_db() as s:
        row = await s.scalar(
            select(Translation).where(Translation.question_id == 1, Translation.lang == "ru")
        )
        row.statement = "Проверено человеком"
        row.reviewed_at = datetime.now(timezone.utc)
        await s.commit()

        question = await s.get(Question, 1)
        await translations.generate(s, question)
        row = await s.scalar(
            select(Translation).where(Translation.question_id == 1, Translation.lang == "ru")
        )
        assert row.statement == "Проверено человеком"


async def test_an_api_failure_leaves_the_question_untranslated(api_db, fake_openai, untranslated):
    """The Italian is the exam language, so a failed translation is a degraded screen and
    never a failed request."""
    fake_openai.error = RuntimeError("Error code: 429 - rate limit")
    async with api_db() as s:
        question = await s.get(Question, 1)
        assert await translations.generate(s, question) is False
        assert (await s.scalars(select(Translation))).all() == []


async def test_a_reply_missing_a_language_stores_the_other(api_db, fake_openai, untranslated):
    fake_openai.reply = {"ru": {"stem": None, "statement": "Только русский"}}
    async with api_db() as s:
        question = await s.get(Question, 1)
        assert await translations.generate(s, question) is True
        langs = {t.lang for t in (await s.scalars(select(Translation))).all()}
    assert langs == {"ru"}


def test_empty_and_unknown_languages_are_dropped():
    parsed = translations.parsed_translations({
        "ru": {"stem": None, "statement": "ок"},
        "en": {"stem": None, "statement": "   "},
        "de": {"stem": None, "statement": "nein"},
    })
    assert set(parsed) == {"ru"}


def test_a_blank_stem_becomes_null_rather_than_an_empty_line():
    parsed = translations.parsed_translations(
        {"ru": {"stem": "  ", "statement": "текст"}}
    )
    assert parsed["ru"]["stem"] is None


# --- who gets one -----------------------------------------------------------

async def test_a_free_user_is_locked_and_costs_nothing(api_db, fake_openai, untranslated):
    broke = Entitlement(has_pass=False, pass_expires_at=None, free_explanations_left=3)
    async with api_db() as s:
        question = await s.get(Question, 1)
        payload, access = await translations.deliver(s, question, reader(), broke)
    assert access is Access.LOCKED
    assert payload["translation"] is None
    assert fake_openai.calls == 0


async def test_translations_switched_off_beats_entitlement(api_db, fake_openai, untranslated):
    async with api_db() as s:
        question = await s.get(Question, 1)
        payload, access = await translations.deliver(
            s, question, reader(on=False), entitled()
        )
    assert access is Access.OFF
    assert fake_openai.calls == 0


async def test_an_italian_reader_is_off_not_unavailable(api_db, fake_openai, untranslated):
    """There is nothing to translate, which is a different thing from a translation
    nobody has written — and it must not cost a call to discover."""
    from api.services.entitlement import translation_offer

    assert translation_offer(entitled(), reader(lang="it"), False) is Access.OFF
    assert translation_offer(entitled(), reader(lang="ru"), False) is Access.AVAILABLE
    assert translation_offer(entitled(), reader(lang="ru"), True) is Access.SHOWN


async def test_delivering_generates_and_returns_the_body(api_db, fake_openai, untranslated):
    async with api_db() as s:
        question = await s.get(Question, 1)
        payload, access = await translations.deliver(s, question, reader(), entitled())
    assert access is Access.SHOWN
    assert payload["translation"]["statement"] == "Изображённый знак запрещает движение"
    assert payload["translation"]["lang"] == "ru"


# --- the question is never delayed ------------------------------------------

async def test_serving_a_question_does_not_wait_for_the_translation(
    client, api_db, fake_openai, untranslated, monkeypatch
):
    """The whole design constraint. The payload says `available` and the client fetches;
    if the question blocked on this, every interaction would gain a few seconds."""
    monkeypatch.setattr(translations, "async_session_factory", lambda: api_db)
    await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    async with api_db() as s:
        user = await s.get(User, 42)
        user.pass_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
        await s.commit()

    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["translation_state"] == "available"
    assert body["translation"] is None

    # Warming ran in the background, so the explicit request is now a cache hit.
    calls_after_warm = fake_openai.calls
    fetched = (await client.post("/users/42/questions/1/translation")).json()
    assert fetched["translation_state"] == "shown"
    assert fetched["translation"]["statement"] == "Изображённый знак запрещает движение"
    assert fake_openai.calls == calls_after_warm


async def test_a_locked_user_is_not_warmed_for(client, api_db, fake_openai, untranslated, monkeypatch):
    monkeypatch.setattr(translations, "async_session_factory", lambda: api_db)
    await client.post("/users", json={"chat_id": 42, "lang": "ru"})
    # New users start on the free trial, so this one must be put past it —
    # "just created" and "free" stopped being the same state when the trial landed.
    await end_trial(api_db, 42)
    body = (await client.get("/users/42/next-question?topic_id=1&exclude_id=2")).json()
    assert body["translation_state"] == "locked"
    assert fake_openai.calls == 0


async def test_a_language_we_do_not_translate_into_never_pays_for_a_call(api_db, monkeypatch):
    """The live money leak: an Italian-UI user asking for a translation.

    `ensure` misses the cache, pays for a generation, and `parsed_translations` then
    drops the result because 'it' is not in TRANSLATION_LANGUAGES - so no row is ever
    written and the next view of the same question pays again. Unbounded.
    """
    from types import SimpleNamespace

    from api.models import Question
    from api.services import translations
    from api.services.entitlement import Access

    calls = []

    async def explode(*a, **kw):
        calls.append(1)
        raise AssertionError("generate() must not be reached for a non-translated language")

    monkeypatch.setattr(translations, "generate", explode)

    async with api_db() as session:
        question = await session.get(Question, 1)
        user = SimpleNamespace(chat_id=1, lang="it", translations_on=True)
        entitlement = SimpleNamespace(can_translate=True)
        payload, access = await translations.deliver(session, question, user, entitlement)

    assert access is Access.OFF
    assert payload["translation"] is None
    assert calls == []


async def test_a_translated_language_still_reaches_generation(api_db, monkeypatch):
    """The guard must not smother the normal path."""
    from types import SimpleNamespace

    from api.models import Question
    from api.services import translations
    from api.services.entitlement import Access

    reached = []

    async def fake_generate(session, question):
        reached.append(question.id)

    monkeypatch.setattr(translations, "generate", fake_generate)

    async with api_db() as session:
        question = await session.get(Question, 2)
        user = SimpleNamespace(chat_id=1, lang="ru", translations_on=True)
        entitlement = SimpleNamespace(can_translate=True)
        _payload, access = await translations.deliver(session, question, user, entitlement)

    assert reached == [2]
    assert access is Access.UNAVAILABLE


# --- the three signing verbs ------------------------------------------------

def test_the_prompt_keeps_the_three_signing_verbs_apart():
    """`preannuncia`, `preavvisa` and `indica` are three different verbs, and the bank
    contains six pairs that differ ONLY in which one is used and carry OPPOSITE answers:

        Il segnale raffigurato preannuncia una curva pericolosa a destra  -> TRUE
        Il segnale raffigurato indica una curva pericolosa a destra       -> FALSE

    Russian and English both reach for one word for all three. When they do, those two
    sentences become identical in translation and the learner sees the same sentence
    twice with opposite correct answers — unlearnable, and it reads as a broken app.

    Measured in the live cache before this rule existed: `preannuncia` came back as
    "indicates" once, "preannounces" three times and "announces" once, and in Russian as
    "предвещает" four times and "предупреждает о" once.
    """
    from api.services.translations import SYSTEM_PROMPT

    assert "preannuncia" in SYSTEM_PROMPT
    assert "preavvisa" in SYSTEM_PROMPT
    assert "предупреждает" in SYSTEM_PROMPT
    assert "gives advance warning of" in SYSTEM_PROMPT
    # The specific collapse to forbid, named in the prompt so the model cannot reach for it.
    assert "preannounces" in SYSTEM_PROMPT, "the calque must be named as forbidden"


def test_the_prompt_shows_a_real_minimal_pair():
    """An abstract instruction is easy to ignore. The pair is quoted verbatim from the
    listato so the model sees the consequence rather than a rule about it."""
    from api.services.translations import SYSTEM_PROMPT

    assert "curva pericolosa a destra" in SYSTEM_PROMPT
    assert "VERO" in SYSTEM_PROMPT and "FALSO" in SYSTEM_PROMPT


def test_temperature_is_dropped_before_reasoning_effort():
    """The order of the fallback ladder is the whole point.

    This was one try/except that fell back to NO parameters. gpt-5-mini rejects
    `temperature=0` outright — "does not support 0 with this model" — so EVERY call took
    that fallback and dropped `reasoning_effort` with it. The constant was set, these tests
    passed, and production never ran a single low-effort translation: 13.8-36.7s each,
    where the same model answers a low-effort request in 2.8-4.2s.

    temperature is the expendable one. It pins determinism, which is nice; effort is most
    of the time a learner spends watching the loading screen.
    """
    import re

    from api.services import translations

    source = open(translations.__file__, encoding="utf-8").read()
    block = source[source.index("attempts = ("):source.index(")", source.index("dict(),"))]

    combos = re.findall(r"dict\(([^)]*)\)", block)
    assert combos, f"the fallback ladder is gone: {block[:200]}"

    first_without_effort = next(
        (i for i, c in enumerate(combos) if "reasoning_effort" not in c), len(combos))
    first_without_temp = next(
        (i for i, c in enumerate(combos) if "temperature" not in c), len(combos))
    assert first_without_temp < first_without_effort, (
        f"reasoning_effort is given up before temperature: {combos}")


async def test_a_temperature_refusal_still_sends_the_effort(monkeypatch):
    """Behavioural version of the above, against the error gpt-5-mini actually returns."""
    from api.models import Question
    from api.services import translations

    seen: list[dict] = []

    class Client:
        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            seen.append(kwargs)
            if kwargs.get("temperature") is not None:
                raise RuntimeError(
                    "Unsupported value: 'temperature' does not support 0 with this model.")

            class R:
                choices = [type("C", (), {"message": type("M", (), {
                    "content": '{"ru": {"statement": "x"}, "en": {"statement": "x"}, '
                               '"uz": {"statement": "x"}}'})()})()]
            return R()

    monkeypatch.setattr(translations, "openai_client", Client)
    monkeypatch.setattr(translations.settings, "openai_api_key", "k")

    # `generate` is the function that talks to the model; it needs a session for the write,
    # and the write is irrelevant here — the assertion is about the parameters sent.
    class Session:
        def add(self, *_a, **_k): pass
        async def commit(self): pass
        async def scalar(self, *_a, **_k): return None
        async def execute(self, *_a, **_k): return None

    await translations.generate(Session(), Question(
        id=1, statement_it="prova", answer=True, quesito_id=1, topic_id=1,
        source_version="v"))

    assert len(seen) >= 2, "it did not retry after the temperature refusal"
    assert seen[-1].get("reasoning_effort") == translations.REASONING_EFFORT, \
        f"the retry dropped reasoning_effort: {seen[-1].keys()}"
