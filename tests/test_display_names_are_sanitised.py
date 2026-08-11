"""The one string one learner types and another learner reads.

Everything else on the leaderboard is a number this server computed. A Telegram first name is
user-controlled text of unspecified length over the whole of Unicode, and it arrived here
with `.strip()[:32]` in front of it and nothing else.

The attack that matters is not script injection — the Mini App renders through `textContent`,
so there is no XSS here. It is the character classes that reach OUT of their own cell:
U+202E reverses everything after it, zero-width characters make a name that looks blank,
newlines make one learner occupy four rows of a shared board, and a leading combining mark
stacks onto whatever the renderer finds to its left, which is somebody else's text.

EMOJI ARE DELIBERATELY KEPT. Somebody can call themselves "🥇 Aziz" and appear to be wearing
a medal — but a filter aggressive enough to catch that mangles ordinary names, and the medal
problem has a better answer: the medal is its own field and its own element on the row, so a
fake one sits visibly in the wrong place. There is a test for that in tests/test_ratings_tab.
"""

from __future__ import annotations

import pytest

from api.services.display_name import MAX_LENGTH, clean


# --- what must survive untouched ----------------------------------------------

@pytest.mark.parametrize("name", [
    "Aziz",
    "Дилноза",                     # Cyrillic: most of this product's users
    "Gʻulom",                      # Uzbek Latin, U+02BB — a modifier letter, not punctuation
    "Zoë",
    "Иван Петров",
    "李明",
    "Aziz 🥇",                     # kept on purpose; see the module docstring
    "O'Brien",
])
def test_ordinary_names_are_left_alone(name):
    assert clean(name) == name


def test_composed_and_decomposed_forms_agree():
    """NFC first, or the same name arrives as two different strings depending on the
    keyboard that typed it — and one of them silently loses a character to truncation."""
    assert clean("Zoë") == clean("Zoë") == "Zoë"


# --- what must not ------------------------------------------------------------

def test_a_right_to_left_override_is_removed():
    """U+202E reverses the rendering of everything after it, so a name can reorder the row
    it sits in — and on a board, the rows around it."""
    assert clean("‮iziA") == "iziA"
    assert "‮" not in (clean("Az‮iz") or "")


@pytest.mark.parametrize("raw", ["​​", "‎", "﻿", "⁦⁩"])
def test_invisible_characters_do_not_make_a_name(raw):
    """A name made of zero-width characters renders as a blank row, which reads as a
    rendering fault rather than as a person."""
    assert clean(raw) is None


def test_newlines_become_a_space_rather_than_disappearing():
    """One learner must not occupy four rows of a shared board. Joined with a space rather
    than removed, because the words were separate and the name should stay readable —
    "Aziz\\n\\nBek" is a person called Aziz Bek, not one called AzizBek."""
    assert clean("Aziz\n\n\nBek") == "Aziz Bek"
    assert clean("Aziz\tBek") == "Aziz Bek"


def test_runs_of_whitespace_collapse():
    assert clean("  Aziz     Bek  ") == "Aziz Bek"


def test_a_leading_combining_mark_is_dropped():
    """With no base character of its own it stacks onto whatever is to its left, which on a
    board is another learner's text."""
    assert clean("́́Aziz") == "Aziz"
    # ...but a mark doing its actual job stays.
    assert clean("Ази́з") == "Ази́з"


def test_a_name_with_no_letters_or_digits_is_not_a_name():
    """One learner is stored as "." in the existing data. On a leaderboard that is
    indistinguishable from a rendering fault, and the anonymous placeholder is both more
    honest and more readable."""
    assert clean(".") is None
    assert clean("...") is None
    assert clean("—") is None
    assert clean("!!!") is None


def test_a_long_name_is_bounded_in_code_points():
    """It is rendered inside other learners' apps and Telegram bounds nothing. Counted in
    code points, not bytes, so the limit means the same thing in Cyrillic as in Latin."""
    assert len(clean("A" * 200)) == MAX_LENGTH
    assert len(clean("д" * 200)) == MAX_LENGTH


def test_nothing_at_all_is_none_not_an_empty_string():
    """`display_name` is nullable and the board renders a neutral placeholder for a learner
    without one. An empty string instead produces a blank row — a different bug wearing the
    first one's clothes."""
    for raw in (None, "", "   ", "\n\n"):
        assert clean(raw) is None, f"{raw!r} produced {clean(raw)!r}"


def test_the_result_is_never_html_escaped_here():
    """The escape belongs where a name is interpolated INTO html — the bot sends
    parse_mode=HTML — not at ingress. Doing it here double-escapes the name in the JSON API,
    where the client renders through textContent and needs the real characters."""
    assert clean("A & B") == "A & B"
    assert clean("<Aziz>") == "<Aziz>"


# --- the ingress path uses it -------------------------------------------------

async def test_the_name_is_cleaned_on_the_way_in(client, registered, api_db):
    """Asserted through the real request, because a sanitiser nothing calls is a module."""
    import json
    import time as clock

    from api.models import User
    from api.services.telegram_auth import sign
    from shared.config import settings

    token = "8918020834:AAEtest-token-not-real-only-for-tests"
    settings.bot_token_prod = token
    settings.env = "prod"
    headers = {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": 42, "first_name": "‮Aziz\n\nBek"},
                            separators=(",", ":")),
         "auth_date": str(int(clock.time()))}, token)}

    assert (await client.get("/webapp/me", headers=headers)).status_code == 200
    async with api_db() as s:
        stored = (await s.get(User, 42)).display_name
    assert stored == "Aziz Bek", f"stored {stored!r}"
