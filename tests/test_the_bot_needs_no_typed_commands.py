"""Everything the bot does is reachable by tapping.

"users usually do not use a /command thinking of commands" — and they are right. The
features were registered as slash-commands and worked perfectly, and a learner handed a
chat window does not think "I wonder if /stats exists". They were invisible in practice.

The commands stay for people who like them. This asserts the tappable surface exists
alongside them, and that the chat's menu button opens the app rather than listing commands.
"""

from __future__ import annotations

import json
import pathlib

from bot import keyboards
from bot.handlers import onboarding
from shared.constants import UI_LANGUAGES

LOCALES = pathlib.Path(__file__).resolve().parent.parent / "bot" / "locales"


def labels(key: str) -> dict[str, str]:
    out = {}
    for lang in UI_LANGUAGES:
        data = json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))
        if key in data:
            out[lang] = data[key]
    return out


# --- the menu exists, in every language --------------------------------------

def test_every_command_has_a_button(monkeypatch):
    """The four things worth doing from the chat. A command with no button is a feature
    only the owner knows about."""
    monkeypatch.setattr(keyboards.settings, "webapp_url", "https://example.test")
    data = keyboards.main_menu("ru").model_dump()
    actions = [b.get("callback_data") for row in data["inline_keyboard"] for b in row]
    for wanted in ("m:stats", "m:plan", "m:settings", "m:help"):
        assert wanted in actions, f"{wanted} has no button: {actions}"


def test_the_menu_opens_the_app(monkeypatch):
    monkeypatch.setattr(keyboards.settings, "webapp_url", "https://example.test")
    data = keyboards.main_menu("ru").model_dump()
    assert any(b.get("web_app") for row in data["inline_keyboard"] for b in row), \
        "the main menu cannot open the Mini App"


def test_the_menu_survives_an_unconfigured_url(monkeypatch):
    """Telegram rejects a web_app button whose url is not https. A deployment without one
    must still get a usable menu rather than an exception on every /start."""
    monkeypatch.setattr(keyboards.settings, "webapp_url", "")
    data = keyboards.main_menu("ru").model_dump()
    actions = [b.get("callback_data") for row in data["inline_keyboard"] for b in row]
    assert "m:stats" in actions
    assert not any(b.get("web_app") for row in data["inline_keyboard"] for b in row)


def test_the_labels_exist_in_every_language():
    """A missing key renders the key itself — "menu_stats" as a button label."""
    for key in ("menu_stats", "menu_plan", "menu_settings", "menu_help", "open_app_short"):
        got = labels(key)
        assert set(got) == set(UI_LANGUAGES), f"{key} missing in {set(UI_LANGUAGES) - set(got)}"
        for lang, text in got.items():
            assert text.strip(), f"{key} is blank in {lang}"


# --- the chat's menu button --------------------------------------------------

def test_start_replaces_the_commands_menu_button():
    """Telegram shows "Menu" — the command list — until a bot says otherwise, and that is
    where a new learner looks first.

    PER CHAT. Setting the DEFAULT is accepted by the API and silently does nothing here:
    getChatMenuButton keeps reporting {"type": "commands"} because the bot registers a
    command list and BotFather's own setting outranks the API. Verified against the live
    bot before this was written.
    """
    source = pathlib.Path(onboarding.__file__).read_text(encoding="utf-8")
    assert "MenuButtonWebApp" in source, "nothing replaces the commands menu button"
    assert "set_chat_menu_button" in source
    assert "chat_id=" in source.split("set_chat_menu_button")[1][:200], \
        "the menu button is being set globally, which does not hold — see the docstring"


def test_start_actually_calls_it():
    """A helper nothing calls is the shape this project has shipped before."""
    source = pathlib.Path(onboarding.__file__).read_text(encoding="utf-8")
    calls = source.count("await offer_the_app(")
    assert calls >= 2, (
        f"offer_the_app is called {calls} time(s) — it belongs on both entry points, "
        f"the returning /start and the end of first-run language choice")
