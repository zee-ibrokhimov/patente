"""The vocabulary list is credited to its author, in the app, in every language.

The glossary was compiled by Zukhriddin Kamolov (Telegram @TTYMI_OKMK2). He gave
permission for this project to use it ON CONDITION that he is credited as its author.

That makes these assertions different in kind from the rest of the suite. Everything else
here protects a learner from a defect; this protects someone outside the project from having
their work used outside the terms it was given under. If the credit stops rendering, the app
is in breach — so it is pinned rather than trusted to survive.

The failure mode being guarded against is not malice. It is a redesign of the vocabulary
screen, an i18n sweep, or a refactor that moves `vocabScreen` and drops one line on the way
— exactly the kind of change nobody would think to check.

See content/VOCAB-CREDITS.md.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from shared.constants import UI_LANGUAGES

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = (ROOT / "webapp/src/main.ts").read_text(encoding="utf-8")
I18N = (ROOT / "webapp/src/i18n.ts").read_text(encoding="utf-8")
CSS = (ROOT / "webapp/src/style.css").read_text(encoding="utf-8")

AUTHOR = "Zukhriddin Kamolov"
HANDLE = "TTYMI_OKMK2"


# --- the name, spelled correctly, in exactly one place ----------------------

def test_the_author_is_named_in_the_app():
    assert AUTHOR in MAIN, "the vocabulary author is not credited anywhere in the Mini App"


def test_the_credit_links_to_the_author():
    """A name with no way to reach the person is a weak credit. `openChat` is the same
    bridge the support and subscribe links use, so it opens in Telegram rather than a
    browser tab."""
    assert HANDLE in MAIN
    assert re.search(rf"openChat\(`https://t\.me/\$\{{VOCAB_AUTHOR\.handle\}}`\)", MAIN), \
        "the credit does not open the author's Telegram"


def test_the_name_lives_in_one_constant_not_four_locales():
    """A person's name is not a translatable string, and four copies is four chances to
    misspell it. Only the surrounding sentence is translated."""
    assert "const VOCAB_AUTHOR" in MAIN
    assert AUTHOR not in I18N, "the author's name was copied into the locale file"
    assert MAIN.count(f'"{AUTHOR}"') == 1, "the name is duplicated; keep one source"


# --- it actually renders ----------------------------------------------------

def test_the_credit_is_rendered_on_the_vocabulary_screen():
    assert "function vocabCredit" in MAIN
    assert re.search(r"wrap\.append\(vocabCredit\(\)\)", MAIN), \
        "vocabCredit is defined but never appended — the credit would not appear"


def test_the_credit_is_shown_on_both_tabs():
    """Appended to the screen wrapper rather than inside the trainer or the list, so it
    shows whether the learner is drilling or browsing."""
    screen = MAIN[MAIN.index("function vocabScreen"):]
    screen = screen[:screen.index("\nfunction vocabCredit")]
    body = screen[screen.index('v.view === "test"'):]
    assert "vocabCredit()" in body, \
        "the credit is appended before the view branch, so one tab may not show it"


def test_the_credit_has_styling_that_keeps_it_legible():
    """A credit rendered at four pixels in the background colour is not a credit. It uses
    the same caption size as the rest of the screen."""
    assert ".v-credit" in CSS
    block = CSS[CSS.index(".v-credit {"):]
    block = block[:block.index("}")]
    assert "--t-caption" in block, "the credit is not using the standard caption size"
    assert "display: none" not in block


# --- in every language ------------------------------------------------------

def block(lang: str) -> dict[str, str]:
    m = re.search(rf'^  {lang}: \{{(.*?)^  \}},', I18N, re.M | re.S)
    assert m, f"no {lang} block"
    return dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)"', m.group(1), re.M))


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_credit_sentence_exists_in_every_language(lang):
    value = block(lang).get("v_credit")
    assert value and value.strip(), f"{lang} has no v_credit — the credit would fall back"


def test_the_sentence_is_translated_not_copied():
    """The failure mode of every i18n edit in this project: one write landing in all four
    slots. A credit that reads in English to an Uzbek learner is a worse credit."""
    values = {lang: block(lang)["v_credit"] for lang in UI_LANGUAGES}
    assert len(set(values.values())) == len(values), \
        f"the credit sentence is shared across locales: {values}"


# --- and the record survives ------------------------------------------------

def test_the_terms_are_written_down():
    """Permission given verbally in a chat is not a record. If the terms are only in
    someone's memory, the next person to touch this cannot honour them."""
    doc = ROOT / "content/VOCAB-CREDITS.md"
    assert doc.exists(), "there is no record of the terms the list was given under"
    text = doc.read_text(encoding="utf-8")
    assert AUTHOR in text
    assert HANDLE in text
    assert "permission" in text.lower()


def test_the_seeder_points_at_the_terms():
    """The seeder is where someone re-importing the list will look, and it is the point at
    which they might reasonably assume the data is ours."""
    seeder = (ROOT / "content/seed_vocab.py").read_text(encoding="utf-8")
    assert AUTHOR in seeder
    assert "VOCAB-CREDITS.md" in seeder
