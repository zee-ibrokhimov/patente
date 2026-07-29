"""Locale integrity.

Interface strings live in JSON from the first commit (plan §12), which means they
can drift silently. These catch the two ways that happens: a key that never got
translated, and a translator dropping a placeholder — the second is worse, because
"Wrong — the answer is {answer}" losing its placeholder produces a verdict that
never tells the user the answer.
"""

import re

import pytest

from bot.i18n import LANGUAGE_NAMES, _strings, missing_keys, normalise, t
from shared.constants import DEFAULT_LANG, LANG_EN, UI_LANGUAGES

PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_locale_has_every_key(lang):
    assert missing_keys()[lang] == set()


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_locale_has_no_extra_keys(lang):
    """An orphan key is usually a rename that only landed in one file."""
    assert set(_strings(lang)) - set(_strings(LANG_EN)) == set()


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_placeholders_match_english(lang):
    reference = _strings(LANG_EN)
    for key, text in _strings(lang).items():
        assert PLACEHOLDER.findall(text).sort() == PLACEHOLDER.findall(reference[key]).sort(), key
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(reference[key])), key


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_every_language_is_offered_a_name(lang):
    assert LANGUAGE_NAMES[lang]


def test_missing_key_falls_back_to_english_not_a_crash():
    assert t("it", "definitely_not_a_key") == "definitely_not_a_key"


def test_formatting_survives_a_bad_placeholder():
    """A locale asking for a variable the caller did not pass must still render."""
    assert t("en", "verdict_wrong")  # called without answer=


def test_placeholder_may_shadow_the_function_parameters():
    """{lang} is a legitimate placeholder name and must not collide with t()'s own
    parameter — this crashed /settings for every user before lang/key were made
    positional-only."""
    assert "Italiano" in t("en", "settings_language", lang="Italiano")
    assert t("en", "settings_language", key="x", lang="Italiano")


@pytest.mark.parametrize(
    "given,expected",
    [("ru", "ru"), ("ru-RU", "ru"), ("en-GB", "en"), ("it", "it"),
     ("pt-BR", DEFAULT_LANG), (None, DEFAULT_LANG), ("", DEFAULT_LANG)],
)
def test_telegram_language_codes_are_normalised(given, expected):
    assert normalise(given) == expected


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_disclaimer_is_present_in_every_language(lang):
    """"Study aid, not an official ministry product" is required in /start and
    /help (plan §5) — and is a consumer-protection point, not decoration."""
    assert len(t(lang, "disclaimer")) > 30
