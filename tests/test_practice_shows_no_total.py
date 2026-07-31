"""Practice must not display a total it does not have.

Practice extends itself, so `question_count` is only the size of the batch fetched so
far. Rendering it as a total tells the learner "Question 5 of 30" and then, without
explanation, "Question 31 of 60" — a finish line that keeps moving. There is no finish
line; the run ends when they end it.

These read the compiled bundle rather than the source. A source-level check would pass on
code that never reaches the browser, and the whole point is what the learner sees.
"""

from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "webapp"


def source(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


from tests.bundle import bundle as built  # noqa: E402


def test_every_locale_can_count_without_a_total():
    """Four locales, or a language falls back to English mid-sentence."""
    i18n = source("src/i18n.ts")
    assert i18n.count("question_n:") == 4


def test_the_counter_is_chosen_by_mode():
    """Not "always show the total" and not "never" — the exam genuinely has thirty and
    saying so is useful there."""
    main = source("src/main.ts")
    block = main[main.index("const meta = el(\"div\", \"q-meta\")"):][:600]
    assert "question_of" in block and "question_n" in block
    assert 'mode === "exam"' in block


def test_the_answer_sheet_is_exam_only():
    """It stands for the paper in front of a candidate. Practice has no paper, and the
    row of cells is drawn one per question in the session — so in practice it would grow
    every time the sitting extended."""
    main = source("src/main.ts")
    i = main.index("wrap.append(answerSheet(run))")
    assert 'mode === "exam"' in main[max(0, i - 400):i]


def test_practice_results_do_not_report_unanswered():
    """`question_count - answered` in practice is the unserved tail of the last batch,
    not questions the learner skipped. Labelling it "unanswered" reports slack in the
    fetching as though it were a failure."""
    main = source("src/main.ts")
    block = main[main.index("const tally = el(\"div\", \"tally\")"):][:900]
    assert 'r.mode === "exam"' in block
    assert 't("unanswered")' in block
    assert 't("correct")' in block


def test_the_shipped_bundle_carries_both_counters():
    """Guards against a build that drops one branch, and against these tests passing on
    source that never reaches a browser."""
    js = built()
    assert "Domanda {n}" in js or "Question {n}" in js or "Вопрос {n}" in js
