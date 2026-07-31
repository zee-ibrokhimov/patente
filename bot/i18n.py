"""Interface strings, loaded from bot/locales/*.json.

Externalised from the first commit (plan §12). The questions are translated by the
content pipeline; this is everything else — buttons, verdicts, the privacy notice,
the disclaimer — and retrofitting hardcoded strings once three languages exist is
the kind of task that never gets done.

A missing key falls back to English rather than raising: a language file that is
one string behind should degrade to a mixed-language screen, not a broken bot.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from shared.constants import DEFAULT_LANG, LANG_EN, UI_LANGUAGES

LOCALES = Path(__file__).parent / "locales"
log = logging.getLogger(__name__)

LANGUAGE_NAMES = {"ru": "Русский", "en": "English", "it": "Italiano", "uz": "O'zbekcha"}


@lru_cache(maxsize=None)
def _strings(lang: str) -> dict[str, str]:
    path = LOCALES / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalise(lang: str | None) -> str:
    """Telegram gives things like 'ru-RU' or 'pt-BR'; keep what we support."""
    if not lang:
        return DEFAULT_LANG
    code = lang.split("-")[0].lower()
    return code if code in UI_LANGUAGES else DEFAULT_LANG


def t(lang: str, key: str, /, **kwargs) -> str:
    """Look up `key` in `lang`, falling back to English.

    `lang` and `key` are positional-only so a placeholder may be named anything —
    including {lang} or {key} — without colliding with these parameters. Without
    that, t(lang, "settings_language", lang=...) raises TypeError, and it raises
    at the point of use rather than at import.
    """
    text = _strings(lang).get(key) or _strings(LANG_EN).get(key)
    if text is None:
        log.warning("missing translation key %r", key)
        return key
    try:
        return text.format(**kwargs) if kwargs else text
    except KeyError:
        log.warning("bad placeholder in %r for %r", key, lang)
        return text


def missing_keys() -> dict[str, set[str]]:
    """Which keys each locale is short of, relative to English. Used by tests."""
    reference = set(_strings(LANG_EN))
    return {lang: reference - set(_strings(lang)) for lang in UI_LANGUAGES}
