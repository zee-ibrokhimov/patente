"""A result is passed, failed, or not a result at all.

`resultsScreen()` derived everything from `const passed = r.passed === true`, which is a
two-way switch fed by a three-way value. Everything that was not exactly `true` went down
the FAIL branch — and `passed` is null for two entirely different reasons:

* a PRACTICE sitting has no `max_errors`, so `_grade` never assigns one. Every practice
  round this app has ever finished was therefore drawn on `.esito.fail` — a pale red card —
  under `icons.cross(44)`, a red cross. A learner who answered ten of ten correctly was
  shown their score in red beneath a cross. Practice is not something you can fail.
* an EXITED exam is ungraded on purpose, and telling someone who just confirmed a dialog
  saying "this will not be counted" that they FAILED is the exact opposite of the promise.

So the tone is chosen from the STATE, not from the grade, and only a graded exam gets a
verdict colour. These are source-level assertions because there is no DOM here; the
rendering itself is checked by eye and by the layout harness.
"""

from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "webapp" / "src"


def results_screen() -> str:
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    start = main.index("function resultsScreen()")
    return main[start:main.index("\nfunction ", start + 10)]


def test_the_tone_is_not_chosen_by_the_grade_alone():
    """`passed ? "pass" : "fail"` on its own is the bug: it has no third outcome and null
    is not false."""
    block = results_screen()
    assert 'esito ${graded ? (passed ? "pass" : "fail") : "dropped"}' in block, (
        "the card's tone must be gated on whether the sitting was graded at all"
    )


def test_practice_is_never_drawn_as_a_failure():
    """The regression that shipped. `graded` has to require exam mode, or practice — whose
    passed is always null — falls back into the fail branch."""
    block = results_screen()
    line = next(ln for ln in block.splitlines() if "const graded" in ln)
    assert 'r.mode === "exam"' in line, line
    assert "!dropped" in line, line


def test_an_exited_exam_is_recognised_by_its_state_not_its_grade():
    """`passed === null` is true of practice too. Keying the third state on the grade would
    make every practice round render as an abandoned exam."""
    block = results_screen()
    line = next(ln for ln in block.splitlines() if "const dropped" in ln)
    assert 'r.state === "abandoned"' in line, line
    assert "passed" not in line, "the third state must not be inferred from the grade"


def test_only_a_graded_exam_gets_a_tick_or_a_cross():
    """A tick congratulates and a cross condemns. Neither is true of a round that was not
    graded, so the other two states get their own glyphs."""
    block = results_screen()
    assert "icons.exit(44)" in block, "an exited sitting should show the door it went out of"
    assert "icons.target(44)" in block, "practice should show what it was aiming at"
    mark = block[block.index("const mark ="):block.index("esito.append(mark)")]
    assert "graded ?" in mark, "the tick/cross pair must be behind the graded check"


def test_unanswered_is_reported_only_where_a_blank_is_a_mistake():
    """In a submitted or expired exam a blank counts against you, so it is worth its own
    figure. An exited sitting never reached those questions — reporting eighteen of them
    would turn stopping early into a score."""
    block = results_screen()
    assert 'r.mode === "exam" && !dropped' in block, (
        'the "unanswered" tally must be suppressed for an exited sitting'
    )


def test_the_client_does_not_keep_its_own_copy_of_the_review_rule():
    """"show questions where user give answer" is enforced on the SERVER, in `results()`,
    and it has to be: every item carries the correct answer, so anything the client merely
    hides is still on the wire. A second copy of the rule here would be the version that
    looks like it is doing the work while the payload leaks anyway."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function reviewList()"):]
    block = block[:block.index("\nfunction ")]
    assert "i.given !== null" not in block, (
        "the client is re-filtering what the server already decided"
    )

    quiz = (ROOT / "api" / "services" / "quiz_sessions.py").read_text(encoding="utf-8")
    body = quiz[quiz.index("async def results("):]
    assert "graded_exam = " in body and "i.given is not None" in body, (
        "the server must be the one deciding which items a review contains"
    )


@pytest.mark.parametrize("key", ["exit_label", "exit_confirm", "not_counted", "not_counted_sub"])
def test_every_language_can_say_it(key):
    """t() falls back to English silently, so a missing key shows English to an Italian
    learner mid-exam with no error anywhere."""
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    assert i18n.count(f"{key}:") == 4, f"{key} is defined {i18n.count(f'{key}:')} times, want 4"


def test_the_confirmation_promises_both_halves():
    """A learner who thinks Exit discards their work will not press it, and one who thinks
    it counts as a failure will not press it either. The dialog has to say both things."""
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    line = next(ln for ln in i18n.splitlines() if ln.strip().startswith("exit_confirm:")
                and "не будет засчитана" in ln)
    assert "ответы" in line, f"the Russian confirmation does not mention seeing the answers: {line}"
