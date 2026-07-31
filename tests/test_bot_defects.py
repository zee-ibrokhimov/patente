"""Regression tests for defects found auditing the bot before building on it.

Each of these was a real, reachable bug. They are grouped here rather than scattered so
the reason each guard exists stays legible.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import keyboards, render
from bot.api_client import ApiError
from bot.i18n import LANGUAGE_NAMES, t
from shared.constants import TRANSLATION_LANGUAGES, UI_LANGUAGES

LOCALES = Path(__file__).resolve().parent.parent / "bot" / "locales"


# --- B3: the Subscribe filter ---------------------------------------------

def test_subscribe_filter_is_a_magic_filter():
    """aiogram calls .resolve() on a callback_data rule; a lambda has no such attribute,
    so the handler raised on every tap. Hidden only because the button is not rendered
    until Tribute is configured - it would have failed first on the day payments went live."""
    from aiogram import F

    from bot.callbacks import Simple

    rule = Simple.filter(F.action == "subscribe").rule
    assert hasattr(rule, "resolve")
    assert rule.resolve(Simple(action="subscribe"))
    assert not rule.resolve(Simple(action="delete_no"))


# --- B5: the language picker must survive a new language ------------------

def test_language_picker_survives_a_language_with_no_display_name(monkeypatch):
    """LANGUAGE_NAMES[code] would KeyError inside the FIRST screen of onboarding, so a
    one-line edit to UI_LANGUAGES in another file took down /start for every new user.

    Uses a code that is deliberately NOT in LANGUAGE_NAMES. This test originally used
    "uz" as its stand-in for an unnamed language and started passing for the wrong reason
    the moment Uzbek was given a real name — a guard whose premise has quietly become
    true tests nothing.
    """
    monkeypatch.setattr("bot.keyboards.UI_LANGUAGES", (*UI_LANGUAGES, "xx"))
    labels = [b.text for row in keyboards.language_picker().inline_keyboard for b in row]
    assert "XX" in labels, "an unnamed language must fall back to its code, not crash"


def test_every_ui_language_has_a_display_name():
    """The fallback above is a safety net, not a licence to ship without a name."""
    missing = [c for c in UI_LANGUAGES if c not in LANGUAGE_NAMES]
    assert missing == [], f"add these to LANGUAGE_NAMES: {missing}"


# --- B8: a setting must not lie about itself ------------------------------

@pytest.mark.parametrize("lang", [c for c in UI_LANGUAGES if c not in TRANSLATION_LANGUAGES])
def test_no_translations_toggle_for_a_language_we_never_translate_into(lang):
    """Italian is a UI language but not a translation target. Offering the toggle let a
    user switch it on, be told it was on, and never see a translation."""
    labels = [b.text for row in keyboards.settings_menu(lang, True).inline_keyboard for b in row]
    assert not any(t(lang, "state_on") in label for label in labels)


@pytest.mark.parametrize("lang", TRANSLATION_LANGUAGES)
def test_the_toggle_is_still_offered_where_it_works(lang):
    labels = [b.text for row in keyboards.settings_menu(lang, True).inline_keyboard for b in row]
    assert any(t(lang, "state_on") in label for label in labels)


def test_settings_text_agrees_with_the_keyboard():
    it = render.settings({"lang": "it", "translations_on": True}, "it")
    ru = render.settings({"lang": "ru", "translations_on": True}, "ru")
    assert t("it", "settings_translations", state=t("it", "state_on")) not in it
    assert t("ru", "settings_translations", state=t("ru", "state_on")) in ru


# --- B6: an outage and a bad request are different --------------------------

@pytest.mark.parametrize(
    "status,transient",
    [(0, True), (500, True), (502, True), (503, True), (400, False), (404, False), (402, False)],
)
def test_only_outages_are_reported_as_retryable(status, transient):
    assert ApiError(status, "x").is_transient is transient


async def test_transport_failures_become_ApiError_not_a_raw_httpx_error():
    """They used to escape the client untouched, so callers had to know about two
    exception hierarchies and the middleware caught only one properly."""
    import httpx

    from bot.api_client import ApiClient

    async def boom(request):
        raise httpx.ConnectError("no route to host")

    client = ApiClient(client=httpx.AsyncClient(transport=httpx.MockTransport(boom),
                                                base_url="http://api"))
    with pytest.raises(ApiError) as caught:
        await client.get_user(1)
    assert caught.value.status == 0
    assert caught.value.is_transient
    await client.close()


# --- B9/B10: locale hygiene ------------------------------------------------

def test_the_sales_copy_does_not_hardcode_the_language_list():
    """plan_perks is the one string that sells the subscription. It named Russian and
    English by hand, so it becomes factually wrong the day a third language ships."""
    for lang in ("it", "ru", "en"):
        perks = t(lang, "plan_perks").lower()
        for named in ("russo", "inglese", "russian", "english", "русский", "английск"):
            assert named not in perks, f"{lang}.plan_perks still enumerates languages"


def test_no_dead_locale_keys():
    """Every key is billed as mandatory translation work for every future language, and
    test_i18n enforces parity - so a dead key is a permanent tax."""
    keys = json.loads((LOCALES / "en.json").read_text(encoding="utf-8")).keys()
    for dead in ("paywall", "btn_settings"):
        assert dead not in keys


def test_the_transient_error_string_exists_everywhere():
    for lang in ("it", "ru", "en"):
        assert t(lang, "error_transient")
        assert t(lang, "error_transient") != t(lang, "error")
