"""Uzbek (beta), and the three ways adding a language silently goes wrong.

Uzbek ships as UI + question translations. Explanations are deliberately NOT written in
Uzbek: a bad translation sits under the Italian where a user can see it is off, but a bad
explanation is the only text on screen and is the thing being sold.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.i18n import LANGUAGE_NAMES
from shared.constants import (
    EXPLANATION_FALLBACK,
    EXPLANATION_LANGUAGES,
    LANG_IT,
    LANG_RU,
    LANG_UZ,
    TRANSLATION_LANGUAGES,
    UI_LANGUAGES,
)

ROOT = Path(__file__).resolve().parent.parent


# --- the three lists mean three different things ---------------------------

def test_uzbek_is_a_ui_and_translation_language_but_not_an_explanation_one():
    assert LANG_UZ in UI_LANGUAGES
    assert LANG_UZ in TRANSLATION_LANGUAGES
    assert LANG_UZ not in EXPLANATION_LANGUAGES


def test_italian_is_never_a_translation_target():
    """The question is already Italian. Asking for an 'it' translation is a paid call
    that can never produce a cacheable row — the live leak fixed earlier."""
    assert LANG_IT not in TRANSLATION_LANGUAGES
    assert LANG_IT in EXPLANATION_LANGUAGES


def test_every_ui_language_can_be_served_an_explanation():
    """Either it is written in that language, or it has a fallback. A UI language with
    neither means choosing it silently switches explanations off."""
    for code in UI_LANGUAGES:
        assert code in EXPLANATION_LANGUAGES or code in EXPLANATION_FALLBACK, code


def test_the_uzbek_fallback_points_somewhere_real():
    target = EXPLANATION_FALLBACK[LANG_UZ]
    assert target == LANG_RU
    assert target in EXPLANATION_LANGUAGES


# --- the warm sentinel, which is what made this a blocker ------------------

async def test_warming_is_not_short_circuited_by_one_cached_language(api_db, monkeypatch):
    """`warm` used to return early if TRANSLATION_LANGUAGES[0] existed. Every question
    already cached in Russian would then be permanently cold for Uzbek, so every reader
    of it pays the foreground latency the module exists to avoid."""
    from sqlalchemy import delete

    from api.models import Translation
    from api.services import translations

    # Question 2 rather than 1: the fixture already seeds a Russian translation for
    # question 1, and inserting a second would hit the (question_id, lang) unique
    # constraint — which is the constraint doing its job, not a bug.
    async with api_db() as s:
        await s.execute(delete(Translation).where(Translation.question_id == 2))
        s.add(Translation(question_id=2, lang="ru", stem=None, statement="кэш"))
        await s.commit()

    called = []

    async def fake_generate(session, question):
        called.append(question.id)

    monkeypatch.setattr(translations, "generate", fake_generate)
    monkeypatch.setattr(translations, "async_session_factory", lambda: api_db)

    await translations.warm(2)
    assert called == [2], "a question cached in ru must still be warmed for uz"


async def test_warming_stops_once_every_language_is_present(api_db, monkeypatch):
    from sqlalchemy import delete

    from api.models import Translation
    from api.services import translations

    async with api_db() as s:
        await s.execute(delete(Translation).where(Translation.question_id == 2))
        for code in TRANSLATION_LANGUAGES:
            s.add(Translation(question_id=2, lang=code, stem=None, statement=f"x-{code}"))
        await s.commit()

    called = []

    async def fake_generate(session, question):
        called.append(question.id)

    monkeypatch.setattr(translations, "generate", fake_generate)
    monkeypatch.setattr(translations, "async_session_factory", lambda: api_db)

    await translations.warm(2)
    assert called == [], "a fully cached question must not be regenerated"


# --- locales --------------------------------------------------------------

def test_uzbek_has_a_display_name():
    """LANGUAGE_NAMES is indexed by the language picker, the first screen of onboarding."""
    assert LANGUAGE_NAMES[LANG_UZ]


def test_the_bot_locale_is_complete():
    """test_i18n enforces parity too, but this names Uzbek specifically so a failure
    reads as 'Uzbek is incomplete' rather than 'some locale is'."""
    en = json.loads((ROOT / "bot/locales/en.json").read_text(encoding="utf-8"))
    uz = json.loads((ROOT / "bot/locales/uz.json").read_text(encoding="utf-8"))
    assert sorted(uz) == sorted(en)
    assert all(v.strip() for v in uz.values())


def test_uzbek_is_written_in_latin_script():
    """Modern Uzbek is Latin. A Cyrillic string here means the model or the author
    reached for the Soviet-era orthography, which reads as wrong to the audience."""
    uz = json.loads((ROOT / "bot/locales/uz.json").read_text(encoding="utf-8"))
    cyrillic = {k: v for k, v in uz.items() if any("Ѐ" <= c <= "ӿ" for c in v)}
    assert cyrillic == {}, f"Cyrillic found in the Uzbek locale: {sorted(cyrillic)}"


def test_the_translation_prompt_asks_for_uzbek_and_pins_the_script():
    """The glossary is the main quality control on translations, and Uzbek needed its own
    column. The script rule matters because a model will happily answer in Cyrillic."""
    source = (ROOT / "api/services/translations.py").read_text(encoding="utf-8")
    assert '"uz": {"stem"' in source, "the prompt must request a uz object"
    assert "UZ: yo'l belgisi" in source, "the glossary needs an Uzbek column"
    assert "ALFABETO LATINO" in source, "the prompt must pin the script"


def test_the_mini_app_carries_every_key_in_uzbek():
    """A partial locale used to be a compile error and t() returned undefined for a
    missing key; both were fixed before this shipped, but the locale should still be
    complete rather than relying on the fallback."""
    source = (ROOT / "webapp/src/i18n.ts").read_text(encoding="utf-8")
    import re

    def keys(lang: str) -> list[str]:
        start = source.index(f"  {lang}: {{")
        return re.findall(r"^\s{4}(\w+):", source[start:source.index("\n  },", start)], re.M)

    assert sorted(keys("uz")) == sorted(keys("en"))
