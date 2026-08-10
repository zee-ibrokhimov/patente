"""The palette keeps its MEANING in both themes.

This file used to assert the app was light-only, and that was the right guard while the
mode cards carried their meaning in their background: a red wash for the exam, green for
practice. Invert those and the signal is gone.

The cards carry a coloured TILE now, so the meaning survives a repaint and dark mode is a
token flip. What still needs guarding is narrower and more important — the ways a theme can
quietly destroy a semantic palette:

  · reading Telegram's `themeParams` and painting the app in the client's arbitrary colours,
    which is NOT the same as reading `colorScheme` to pick one of our two designed themes;
  · writing custom properties inline on documentElement, which outranks every rule in the
    stylesheet, so the tokens lose without anything failing. That is the exact mechanism
    that broke this once already.

WORTH RECORDING: the old version of this file passed against the dark theme by accident. It
asserted on `data-scheme` and `dataset.scheme`, and the implementation happened to use
`data-theme` and `dataset.theme`. One attribute name apart. A guard that passes by
coincidence is not a guard, and the only reason it was noticed is that a reviewer went
looking for why it had not failed.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "webapp"


def source(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def code_only(text: str) -> str:
    """Strip comment lines, so a mention in prose does not count as a use."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.strip().startswith(("*", "/*", "//"))
    )


def test_telegram_theme_params_are_never_read():
    """`colorScheme` says light or dark and is read on purpose. `themeParams` is the
    client's own palette — reading it repaints the app in colours nobody designed, and the
    semantic meanings go with them."""
    assert "themeParams" not in code_only(source("src/telegram.ts"))


def test_no_inline_css_variable_is_ever_written():
    """The exact mechanism that broke this once: an inline custom property on
    documentElement outranks every rule in the stylesheet, so the tokens lose and nothing
    fails."""
    assert not re.search(r"style\.setProperty\(\s*['\"]--", code_only(source("src/telegram.ts")))


def test_both_themes_exist():
    css = source("src/tokens.css")
    assert ":root {" in css, "the light palette is gone"
    assert '[data-theme="dark"]' in css, "there is no dark palette"


@pytest.mark.parametrize("token", ["--bg", "--card", "--text", "--edge", "--accent"])
def test_every_core_token_has_a_dark_value(token):
    """A token defined only in :root keeps its LIGHT value under the dark theme — a white
    card on a near-black page, with no error anywhere."""
    css = source("src/tokens.css")
    dark = css[css.index('[data-theme="dark"]'):]
    assert re.search(rf"{re.escape(token)}\s*:", dark), f"{token} has no dark value"


def test_the_document_declares_both_schemes():
    """Declaring only one makes the browser paint the scroll gutter and form controls for
    the wrong theme until the script runs."""
    assert 'content="light dark"' in source("index.html")


def test_the_dark_chrome_matches_the_dark_background():
    """The app tells Telegram to paint ITS chrome to match us. If the two drift, a
    dark-mode user gets a mismatched header above the app, which reads as broken rather
    than deliberate — the same failure the light version of this test guarded."""
    tg = source("src/telegram.ts")
    chrome = re.search(r'theme === "dark" \? "(#[0-9a-fA-F]{6})"', tg)
    css = source("src/tokens.css")
    dark = css[css.index('[data-theme="dark"]'):]
    token = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", dark)
    assert chrome and token, "could not find both values to compare"
    assert chrome.group(1).lower() == token.group(1).lower(), (
        f"Telegram chrome {chrome.group(1)} does not match --bg {token.group(1)} in dark")


def test_the_light_chrome_still_matches_the_light_background():
    chrome = re.search(r'CHROME_BG\s*=\s*"(#[0-9a-fA-F]{6})"', source("src/telegram.ts"))
    token = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", source("src/tokens.css"))
    assert chrome and token
    assert chrome.group(1).lower() == token.group(1).lower()
