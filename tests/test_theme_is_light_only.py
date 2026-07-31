"""The Mini App must not follow Telegram's theme.

A comment saying "we ignore themeParams" is worth nothing on its own: the previous
version carried exactly that claim in tokens.css while telegram.ts quietly overrode it,
writing Telegram's colours as INLINE styles on documentElement — and inline styles beat
`:root`. A user with Telegram in dark mode therefore got a dark app while the stylesheet
insisted it was light, with no error anywhere.

So these tests read the code rather than the intent.

Why light-only is a design decision rather than a preference: the palette carries meaning.
Soft red means "the test you can fail", soft green means practice, and gold means Premium
and nothing else. A dark repaint turns all three from signals into decoration.
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
    assert "themeParams" not in code_only(source("src/telegram.ts"))


def test_no_inline_css_variable_is_ever_written():
    """The exact mechanism that broke it, and the only one that could break it again
    silently: an inline custom property on documentElement outranks every rule in the
    stylesheet, so the tokens lose without anything failing."""
    assert not re.search(r"style\.setProperty\(\s*['\"]--", code_only(source("src/telegram.ts")))


def test_no_colour_scheme_attribute_is_set():
    assert "dataset.scheme" not in code_only(source("src/telegram.ts"))


@pytest.mark.parametrize("name", ["src/style.css", "src/tokens.css"])
def test_the_stylesheets_do_not_react_to_a_dark_scheme(name):
    css = source(name)
    assert "prefers-color-scheme" not in css
    assert not re.search(r"\[data-scheme\s*=\s*[\"']dark", css)


def test_the_document_declares_light():
    assert 'content="light"' in source("index.html")


def test_the_built_bundle_carries_no_dark_rule():
    """Guards against a dependency or a future stylesheet reintroducing one after the
    source itself looks clean."""
    built = sorted((WEB / "dist/assets").glob("*.css"))
    if not built:
        pytest.skip("no build output — run npm run build")
    assert "prefers-color-scheme" not in built[-1].read_text(encoding="utf-8")


def test_the_chrome_colour_matches_the_background_token():
    """The app tells Telegram to paint ITS chrome to match us, which is the opposite
    direction of travel from following a theme. If these two values drift, a dark-mode
    user gets a mismatched header above a light app, which reads as broken rather than
    deliberate."""
    chrome = re.search(r'CHROME_BG\s*=\s*"(#[0-9a-fA-F]{6})"', source("src/telegram.ts"))
    token = re.search(r"--bg:\s*(#[0-9a-fA-F]{6})", source("src/tokens.css"))
    assert chrome and token, "could not find both values to compare"
    assert chrome.group(1).lower() == token.group(1).lower()
