"""Paying once for a translation, and not paying forever for one that never comes.

Two faults in the same function, both about money.

NO LOCK. `ensure` checked for a row, and if it was missing called `generate`. Explanations
have had a per-cluster lock since they were written; translations never got one. The first
question of every session is requested by the client at the same moment it is warmed in
the background, so both found nothing and both called OpenAI — two paid calls for one
result, on every single session start.

NO CEILING. `generate` writes whatever the model returned. If a language did not come back,
the row stays missing, and the NEXT request finds it missing and generates again. A
question the model would not produce Uzbek for would be re-translated on every request from
every Uzbek reader, forever, and the row would never appear. Found with 38 real questions
that had ru and en and no uz.
"""

from __future__ import annotations

import asyncio

import pytest

from api.models import Question, Translation
from api.services import translations


@pytest.fixture(autouse=True)
def _clean_state():
    translations._locks.clear()
    translations._missing.clear()
    yield
    translations._locks.clear()
    translations._missing.clear()


@pytest.fixture
async def question(api_db):
    async with api_db() as s:
        return await s.get(Question, 3)


# --- one call, not two ------------------------------------------------------

async def test_concurrent_readers_share_one_call(api_db, question, monkeypatch):
    """A Russian and an Uzbek reader arriving together must wait on the same call. One
    call produces every language, so a second is pure waste."""
    calls = []

    async def counting(session, q, model=None):
        calls.append(q.id)
        await asyncio.sleep(0.05)          # a real call is seconds, not instant
        session.add(Translation(question_id=q.id, lang="ru", statement="перевод"))
        session.add(Translation(question_id=q.id, lang="uz", statement="tarjima"))
        await session.commit()
        return True

    monkeypatch.setattr(translations, "generate", counting)

    async with api_db() as s:
        q = await s.get(Question, 3)
        await asyncio.gather(
            translations.ensure(s, q, "ru"),
            translations.ensure(s, q, "uz"),
        )
    assert len(calls) == 1, f"paid {len(calls)} times for one translation"


async def test_a_cached_translation_makes_no_call(api_db, question, monkeypatch):
    async def boom(*a, **kw):
        raise AssertionError("generate must not be called when the row exists")

    monkeypatch.setattr(translations, "generate", boom)
    async with api_db() as s:
        assert await translations.ensure(s, await s.get(Question, 1), "ru") is not None


# --- and not forever --------------------------------------------------------

async def test_a_language_that_never_arrives_is_not_retried_immediately(
        api_db, question, monkeypatch):
    """THE cost bug. Without this, every Uzbek reader of that question pays again."""
    calls = []

    async def partial(session, q, model=None):
        calls.append(q.id)
        session.add(Translation(question_id=q.id, lang="ru", statement="только русский"))
        await session.commit()
        return True

    monkeypatch.setattr(translations, "generate", partial)

    async with api_db() as s:
        q = await s.get(Question, 3)
        assert await translations.ensure(s, q, "uz") is None
        assert await translations.ensure(s, q, "uz") is None
        assert await translations.ensure(s, q, "uz") is None
    assert len(calls) == 1, f"a missing language was regenerated {len(calls)} times"


async def test_the_other_languages_still_work_after_one_fails(api_db, monkeypatch):
    """Remembering that Uzbek did not arrive must not block Russian, which did."""
    async def partial(session, q, model=None):
        session.add(Translation(question_id=q.id, lang="ru", statement="есть"))
        await session.commit()
        return True

    monkeypatch.setattr(translations, "generate", partial)
    async with api_db() as s:
        q = await s.get(Question, 3)
        assert await translations.ensure(s, q, "uz") is None
        assert await translations.ensure(s, q, "ru") is not None


def test_the_ceiling_expires():
    """An hour later it is worth another try — a model that failed once may not fail
    again, and this is a cost guard rather than a permanent verdict."""
    translations._missing[(1, "uz")] = 0.0
    assert translations._recently_failed(1, "uz", now=10.0) is True
    assert translations._recently_failed(1, "uz", now=translations.RETRY_AFTER + 10) is False


def test_a_language_never_asked_for_is_not_marked_failed():
    assert translations._recently_failed(42, "uz") is False


async def test_a_failure_does_not_block_a_different_question(api_db, monkeypatch):
    """The key is (question, language). Marking a whole language as broken because one
    question failed would turn one bad row into an outage for that language."""
    translations._missing[(3, "uz")] = 9e9
    calls = []

    async def counting(session, q, model=None):
        calls.append(q.id)
        session.add(Translation(question_id=q.id, lang="uz", statement="tarjima"))
        await session.commit()
        return True

    monkeypatch.setattr(translations, "generate", counting)
    async with api_db() as s:
        assert await translations.ensure(s, await s.get(Question, 4), "uz") is not None
    assert calls == [4]
