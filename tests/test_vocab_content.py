"""The shipped word list itself, checked as data.

tests/test_vocab_grading.py proves the grader behaves correctly on hand-made examples.
This file proves the same guarantees hold for the 1104 entries actually in
content/vocab.json — which is where they can quietly stop holding, because that file is
regenerated whenever the owner re-exports the sheet, and a regeneration is exactly the
moment nobody is looking.

The entries at ranks 1-15 are not from the sheet. They are the legal terms that were
hand-verified against real model output while translation was being built (STATUS.md
§15) and which, until now, lived only inside the translation prompt — visible to the
model and never to the learner drilling vocabulary.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from api.services.vocab_grading import Verdict, accepted_answers, grade

DATA = pathlib.Path(__file__).resolve().parent.parent / "content" / "vocab.json"
TERMS = json.loads(DATA.read_text(encoding="utf-8"))
BY_IT = {t["it"].lower(): t for t in TERMS}

CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def test_the_list_is_the_size_it_claims():
    assert len(TERMS) > 1000


@pytest.mark.parametrize("lang", ["it", "en", "ru", "uz"])
def test_no_entry_is_missing_a_language(lang):
    """A term with an empty gloss cannot be asked in that direction, and would grade
    every answer to it as wrong — a word the learner can never get right."""
    missing = [t["it"] for t in TERMS if not str(t.get(lang, "")).strip()]
    assert missing == [], f"{len(missing)} entries have no {lang}: {missing[:5]}"


def test_no_italian_term_appears_twice():
    seen, dupes = set(), []
    for t in TERMS:
        key = t["it"].strip().lower()
        if key in seen:
            dupes.append(t["it"])
        seen.add(key)
    assert dupes == [], f"duplicate Italian: {dupes[:5]}"


def test_ranks_are_unique():
    ranks = [t["rank"] for t in TERMS]
    assert len(ranks) == len(set(ranks))


def test_uzbek_is_latin_throughout():
    """Latin is the official script in Uzbekistan. A Cyrillic entry would also be
    ungradeable against a learner typing Latin."""
    cyrillic = [t["it"] for t in TERMS if CYRILLIC.search(t["uz"])]
    assert cyrillic == [], f"Uzbek in Cyrillic for: {cyrillic[:5]}"


def test_no_gloss_is_the_bare_word_signal():
    """`сигнал` / `signal` is the specific mistranslation this project has fought since
    gpt-4o-mini rendered `il segnale raffigurato` as "сигнал". As a whole gloss it is
    always wrong: a road sign is знак, a traffic light светофор, a horn звуковой сигнал."""
    bad = [t["it"] for t in TERMS
           if t["ru"].strip().lower() == "сигнал" or t["en"].strip().lower() == "signal"]
    assert bad == [], f"bare signal/сигнал used as a gloss for: {bad}"


# --- the legal terms that must be present ----------------------------------

CORE = ["segnale", "segnale luminoso", "carreggiata", "corsia", "banchina",
        "centro abitato", "sorpasso", "precedenza", "arresto", "fermata", "sosta",
        "autocarro", "autovettura", "ciclomotore", "motociclo"]


@pytest.mark.parametrize("term", CORE)
def test_the_core_legal_terms_are_in_the_word_list(term):
    """These are what the written exam actually turns on. They were absent from the
    owner's frequency sheet — only `sosta` appeared — so a learner could drill all 1090
    words and never meet `segnale` or `precedenza`."""
    assert term in BY_IT, f"{term} is missing from the vocabulary"


@pytest.mark.parametrize("term", CORE)
def test_the_core_terms_come_first(term):
    assert BY_IT[term]["rank"] <= len(CORE)


def test_segnale_is_a_sign_and_not_a_signal():
    t = BY_IT["segnale"]
    assert t["ru"] == "знак"
    assert t["en"] == "sign"


def test_the_traffic_light_is_not_confused_with_the_sign():
    assert BY_IT["segnale luminoso"]["ru"] == "светофор"
    assert BY_IT["segnale"]["ru"] != BY_IT["segnale luminoso"]["ru"]


# --- the three that must stay apart, checked on the REAL glosses -----------

TRIO = ["arresto", "fermata", "sosta"]


@pytest.mark.parametrize("a", TRIO)
@pytest.mark.parametrize("b", TRIO)
def test_the_legally_distinct_trio_never_grades_as_a_near_miss(a, b):
    """Against the shipped glosses, not against fixtures.

    Both directions matter. Typing `fermata` when the answer is `sosta` must be WRONG,
    and so must answering with fermata's Russian when sosta's was wanted — otherwise the
    trainer tells a learner they were "almost right" about a distinction that decides
    real exam questions.
    """
    if a == b:
        return
    assert grade(BY_IT[a]["it"], BY_IT[b]["it"], lang="it").verdict is Verdict.WRONG
    for lang in ("ru", "en"):
        assert grade(BY_IT[a][lang], BY_IT[b][lang], lang=lang).verdict is Verdict.WRONG


@pytest.mark.parametrize("term", TRIO)
def test_each_of_the_trio_still_accepts_its_own_answer(term):
    """The test above would also pass if the grader rejected everything."""
    for lang in ("it", "ru", "en", "uz"):
        value = BY_IT[term]["it"] if lang == "it" else BY_IT[term][lang]
        first = accepted_answers(value)[0]
        assert grade(first, value, lang=lang).verdict is Verdict.CORRECT


def test_arresto_accepts_the_short_form_too():
    """Stored as `полная остановка, остановка`: a learner who types just `остановка`
    has given a right answer, and the alternative exists so they are not marked down."""
    assert grade("остановка", BY_IT["arresto"]["ru"], lang="ru").verdict is Verdict.CORRECT
    assert grade("полная остановка", BY_IT["arresto"]["ru"], lang="ru").verdict is Verdict.CORRECT


def test_no_gloss_leaves_a_parenthetical_the_learner_would_have_to_type():
    """`turish (parking)` is a fine note for a translator and an impossible answer for
    someone typing on a phone. Those were rewritten as comma alternatives."""
    bracketed = [t["it"] for t in TERMS
                 if any("(" in str(t[k]) for k in ("en", "ru", "uz"))]
    assert bracketed == [], f"parenthetical glosses remain: {bracketed[:5]}"


def test_the_core_terms_are_spelled_consistently():
    """All fifteen come from one hand-verified table, so they should look like one block
    in the word list. `sosta` also exists in the owner's sheet as `Sosta`; the seed
    matches case-insensitively (so progress survives a re-export) but must converge on
    the file's spelling, or that one row keeps its capital forever."""
    core = [t["it"] for t in TERMS if t["rank"] <= len(CORE)]
    assert core == [c.lower() for c in core], f"inconsistent casing: {core}"
