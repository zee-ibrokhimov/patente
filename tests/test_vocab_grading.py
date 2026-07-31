"""The vocabulary test grades typed answers, so it has to judge near-misses.

The dangerous direction is ALMOST firing when it should not. WRONG shown for a right
answer is annoying and obvious, and the learner will complain. ALMOST shown for a
genuinely different word is silent: the app says "almost right!" to someone who did not
know the answer, and they carry the mistake forward believing it was a typo.

So most of what follows pins down what must NOT be treated as a near-miss — in
particular `sosta` against `fermata`, which the rest of this project exists to keep apart.
"""

from __future__ import annotations

import pytest

from api.services.vocab_grading import (
    Grade,
    Verdict,
    accepted_answers,
    edit_distance,
    grade,
    normalise,
)


# --- exactly right ----------------------------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("sosta", "sosta"),
    ("SOSTA", "sosta"),
    ("  sosta  ", "sosta"),
    ("Sosta.", "sosta"),
    ("la sosta", "sosta"),          # article typed
    ("sosta", "la sosta"),          # article stored
    ("proezжая часть".replace("з", "з"), "проезжая часть") if False else ("проезжая часть", "проезжая часть"),
])
def test_these_are_correct(given, expected):
    assert grade(given, expected).verdict is Verdict.CORRECT


def test_an_article_alone_is_not_stripped_into_nothing():
    """Stripping articles must not empty a one-word answer that IS an article."""
    assert normalise("il", "it") == "il"


def test_any_stored_alternative_counts():
    assert grade("glare", "Dazzle, glare").verdict is Verdict.CORRECT
    assert grade("dazzle", "Dazzle, glare").verdict is Verdict.CORRECT


def test_the_correction_shown_is_the_alternative_actually_matched():
    """A learner who nearly typed 'glare' should not be corrected to 'dazzle'."""
    g = grade("glar", "Dazzle, glare")
    assert g.verdict is Verdict.ALMOST
    assert g.correction == "glare"


@pytest.mark.parametrize("typed", ["to'xtash", "to’xtash", "toʻxtash"])
def test_uzbek_accepts_every_apostrophe_a_phone_produces(typed):
    """o' is written with U+02BB, U+2019 or a plain quote depending on the keyboard.
    Which one the device emitted is not something to examine a learner on."""
    assert grade(typed, "to'xtash", lang="uz").verdict is Verdict.CORRECT


# --- the right word, the wrong form ----------------------------------------

@pytest.mark.parametrize("given, expected", [
    ("vietata", "vietato"),          # gender
    ("fermate", "fermata"),          # number
    ("accompagnate", "accompagnato"),
    ("consentita", "consentito"),
    ("perche", "perché"),            # missing accent
    ("distanza di sicurezze", "distanza di sicurezza"),
])
def test_these_are_near_misses(given, expected):
    g = grade(given, expected)
    assert g.verdict is Verdict.ALMOST
    assert g.correction == expected


def test_a_near_miss_still_counts_as_progress():
    """They produced the word and missed the ending. Marking that as failure would send
    them back to box one for something they know."""
    assert grade("vietata", "vietato").is_progress
    assert grade("vietato", "vietato").is_progress
    assert not grade("obbligatorio", "vietato").is_progress


def test_a_near_miss_always_carries_the_correction():
    """The whole value of ALMOST over CORRECT is that the right spelling gets shown."""
    assert grade("vietata", "vietato").correction == "vietato"
    assert grade("vietato", "vietato").correction is None


# --- a different word is a different word -----------------------------------

@pytest.mark.parametrize("given, expected", [
    ("cosa", "casa"),                # one edit apart, unrelated
    ("casa", "cosa"),
    ("destra", "sinistra"),          # opposites
    ("sinistra", "destra"),
    ("fermata", "sosta"),            # legally distinct — the point of the glossary
    ("sosta", "fermata"),
    ("arresto", "fermata"),
    ("sosta", "arresto"),
    ("segnale", "semaforo"),
    ("vietato", "obbligatorio"),
    ("", "sosta"),
    ("qwerty", "sosta"),
])
def test_these_must_never_be_called_almost_correct(given, expected):
    assert grade(given, expected).verdict is Verdict.WRONG


def test_the_three_legally_distinct_words_are_mutually_wrong():
    """arresto, fermata and sosta are three different things in Italian law. If the
    grader treats any pair as a near-miss it actively teaches the confusion this app
    exists to prevent."""
    for a in ("arresto", "fermata", "sosta"):
        for b in ("arresto", "fermata", "sosta"):
            if a != b:
                assert grade(a, b).verdict is Verdict.WRONG, f"{a} vs {b}"


@pytest.mark.parametrize("given, expected", [
    ("circolazione", "circolare"),        # noun vs verb, 7 shared characters
    ("attraversamento", "attraversare"),  # 10 shared
    ("illuminazione", "illuminato"),
    ("velocemente", "veloce"),
])
def test_a_shared_stem_is_not_enough_on_its_own(given, expected):
    """These pairs all PASS the shared-stem rule — a long common prefix — and are
    rejected only by the edit-distance rule. Written this way on purpose: the obvious
    candidate (`porta` / `portafoglio`) is caught by the prefix rule too, so it proves
    nothing about distance. Deleting the distance check must make these fail.
    """
    assert grade(given, expected).verdict is Verdict.WRONG


def test_a_short_distance_is_not_enough_on_its_own():
    """`casa`/`cosa` is a single edit. Only the shared-stem rule separates them, so this
    fails if that rule is ever dropped."""
    assert edit_distance("casa", "cosa") == 1
    assert grade("cosa", "casa").verdict is Verdict.WRONG


# --- the machinery ----------------------------------------------------------

def test_edit_distance_gives_up_rather_than_grinding():
    assert edit_distance("a" * 40, "b" * 40, cap=4) == 5


def test_alternatives_split_on_every_separator_the_sheet_uses():
    assert accepted_answers("Dazzle, glare") == ["Dazzle", "glare"]
    assert accepted_answers("stop; halt") == ["stop", "halt"]
    assert accepted_answers("speed / pace") == ["speed", "pace"]
    assert accepted_answers("") == []
    assert accepted_answers("  ") == []


def test_an_empty_stored_gloss_cannot_be_answered_correctly():
    """A term with no translation must never grade as correct, whatever is typed."""
    assert grade("anything", "").verdict is Verdict.WRONG
    assert grade("", "").verdict is Verdict.WRONG


def test_grade_is_immutable():
    g = grade("sosta", "sosta")
    with pytest.raises(Exception):
        g.verdict = Verdict.WRONG  # type: ignore[misc]


@pytest.mark.parametrize("lang", ["it", "en", "ru", "uz"])
def test_every_served_language_grades_without_error(lang):
    assert isinstance(grade("qualcosa", "qualcos'altro", lang=lang), Grade)
