"""Regression gate on the extracted bank itself.

extract.py validates before writing, but these run against the committed
questions.json so a regression in the extractor cannot land quietly.
"""

import collections
import json
import re

import pytest

from shared.config import (
    CONTENT_OUT,
    EXPECTED_QUESITI,
    EXPECTED_STATEMENTS,
    EXPECTED_TOPICS,
    QUESTIONS_JSON,
    SOURCE_VERSION,
)

pytestmark = pytest.mark.skipif(
    not QUESTIONS_JSON.exists(), reason="run content/extract.py first"
)


@pytest.fixture(scope="module")
def bank():
    return json.loads(QUESTIONS_JSON.read_text(encoding="utf-8"))


def test_counts_match_the_listato(bank):
    assert bank["counts"]["statements"] == EXPECTED_STATEMENTS
    assert bank["counts"]["quesiti"] == EXPECTED_QUESITI
    assert bank["counts"]["topics"] == EXPECTED_TOPICS
    assert bank["source_version"] == SOURCE_VERSION
    assert len(bank["questions"]) == EXPECTED_STATEMENTS


def test_every_statement_is_well_formed(bank):
    for q in bank["questions"]:
        assert isinstance(q["answer"], bool), q["id"]
        assert q["statement_it"].strip(), q["id"]
        assert q["topic"].strip(), q["id"]
        assert q["quesito_id"], q["id"]


def test_statement_ids_are_unique(bank):
    ids = [q["id"] for q in bank["questions"]]
    assert len(set(ids)) == len(ids)


def test_answers_are_not_lopsided(bank):
    """A parser that borrowed the neighbouring row's answer would skew this."""
    counts = collections.Counter(q["answer"] for q in bank["questions"])
    assert counts[True] == 3542
    assert counts[False] == 3564


def test_no_statement_leaked_the_answer_column(bank):
    """Defect A: VERO/FALSO merges into the statement line on 68 rows."""
    for q in bank["questions"]:
        assert not re.search(r"\b(VERO|FALSO)\s*$", q["statement_it"]), q["id"]


def test_no_statement_absorbed_a_topic_continuation(bank):
    """Defect C: wrapped topic lines run through the statement column."""
    for q in bank["questions"]:
        assert "convivenza civile" not in q["statement_it"], q["id"]
        assert "campo visivo del cond." not in q["statement_it"], q["id"]


def test_figure_referencing_statements_have_a_figure(bank):
    pattern = re.compile(
        r"raffigurat|in figura|di figura|figura rappresenta|segnale \(|pannell[oi] \(", re.I
    )
    orphans = [q["id"] for q in bank["questions"]
               if pattern.search(q["statement_it"]) and not q["image"]]
    assert orphans == []


def test_every_referenced_figure_exists_on_disk(bank):
    missing = [q["image"] for q in bank["questions"]
               if q["image"] and not (CONTENT_OUT / q["image"]).exists()]
    assert missing == []


def test_composite_figures_are_preserved(bank):
    """Comparison items carry their own figure, differing from their group's.

    "Il segnale (A) può essere abbinato al segnale (B)" ships a two-sign composite.
    Collapsing a group onto one figure would show the wrong image for these.
    """
    by_quesito = collections.defaultdict(set)
    for q in bank["questions"]:
        if q["image"]:
            by_quesito[q["quesito_id"]].add(q["image"])

    assert len(by_quesito[4061]) == 3
    assert len(by_quesito[4070]) == 3
    assert len(by_quesito[4081]) == 3
    assert sum(1 for v in by_quesito.values() if len(v) > 1) == 45

    q19097 = next(q for q in bank["questions"] if q["id"] == 19097)
    q19096 = next(q for q in bank["questions"] if q["id"] == 19096)
    assert q19097["quesito_id"] == q19096["quesito_id"]
    assert q19097["image"] != q19096["image"]


def test_encoding_artefacts_are_normalised(bank):
    """U+00BF stood in for an apostrophe in the source topic names."""
    for topic in bank["topics"]:
        assert "¿" not in topic["name"]
    assert any("dell'ambiente" in t["name"] for t in bank["topics"])


def test_quesito_membership_covers_every_statement(bank):
    grouped = {sid for q in bank["quesiti"] for sid in q["statements"]}
    assert grouped == {q["id"] for q in bank["questions"]}
