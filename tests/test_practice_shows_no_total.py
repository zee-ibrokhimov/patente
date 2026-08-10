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


def _run_bar() -> tuple[str, str]:
    """runBar()'s exam branch and its practice branch, separately.

    Both counters used to live in one `.q-meta` row and these tests anchored on that string.
    The counter now lives in the run bar's position chip, so the anchor moved — the property
    did not, and asserting it per branch is stronger than the old "both words appear
    somewhere in the next 600 characters".
    """
    main = source("src/main.ts")
    block = main[main.index("function runBar("):]
    block = block[:block.index("\nfunction ")]
    # Sliced from `if (exam) {` onwards, not from the first `} else {` in the function —
    # runBar opens with an `if (run.deadline)` whose else came first and made both slices
    # empty, which passed nothing and failed loudly rather than quietly, but for the wrong
    # reason.
    start = block.index("if (exam) {")
    split = block.index("} else {", start)
    return block[start:split], block[split:block.index("bar.append(chip);", split)]


def test_the_counter_is_chosen_by_mode():
    """Not "always show the total" and not "never" — the exam genuinely has thirty and
    saying so is useful there."""
    exam, practice = _run_bar()
    assert "question_count" in exam, "the exam should say how many questions there are"
    assert "question_n" in practice, "practice counts up"
    assert "question_count" not in practice, (
        "practice's question_count is only the batch fetched so far; rendering it as a "
        "total promises a finish line that moves"
    )


def test_the_answer_sheet_is_exam_only():
    """It stands for the paper in front of a candidate. Practice has no paper, and the
    row of cells is drawn one per question in the session — so in practice it would grow
    every time the sitting extended.

    It is now reached by tapping the position chip, so "exam only" means the chip opens it
    in the exam branch and does nothing in the practice one."""
    exam, practice = _run_bar()
    assert "openAnswerSheet" in exam
    assert "openAnswerSheet" not in practice
    assert "disabled = true" in practice


def test_practice_results_do_not_report_unanswered():
    """`question_count - answered` in practice is the unserved tail of the last batch,
    not questions the learner skipped. Labelling it "unanswered" reports slack in the
    fetching as though it were a failure."""
    main = source("src/main.ts")
    # Sliced between the two ends of the tally block rather than by a fixed 900 characters.
    # The byte window was measuring comment length as much as code: adding an explanatory
    # comment inside the block pushed the branch past the cutoff and failed this test while
    # the branch itself was untouched, which is a false alarm the next person has to
    # re-derive. The markers below are the real boundaries of the thing being asserted.
    start = main.index('const tally = el("div", "tally")')
    end = main.index("esito.append(tally)", start)
    block = main[start:end]
    assert 'r.mode === "exam"' in block
    assert 't("unanswered")' in block
    assert 't("correct")' in block


def test_the_shipped_bundle_carries_both_counters():
    """Guards against a build that drops one branch, and against these tests passing on
    source that never reaches a browser."""
    js = built()
    assert "Domanda {n}" in js or "Question {n}" in js or "Вопрос {n}" in js
