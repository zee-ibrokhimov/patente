"""The translation switch, in both directions, and the end of a completed paper.

Four reports, all about a control doing less than it appears to:

1. VERO / FALSO are printed on the real exam paper. Drilling for months on ВЕРНО/НЕВЕРНО
   and meeting VERO/FALSO for the first time in the exam room is the one thing this app
   exists to prevent, so the answer buttons stay Italian in every UI language.

2. Turning translations ON mid-sitting fetched exactly one — the question on screen. A quiz
   started with the switch down was PREPARED with it down: the opening prefetch skipped
   translations, so the next four questions arrived untranslated too, one wait at a time.

3. Turning them OFF left the current one on screen until the next question. The switch
   appeared not to work, and then to work one question later.

4. With every question answered and time still on the clock there was no way to hand in
   from the question screen at all — Next wrapped round the paper, and Submit was two taps
   away behind the position chip.

These are source-level assertions: the behaviours live in the client, and there is no DOM
here. What each one pins is the specific mechanism, not the wording.
"""

from __future__ import annotations

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"
LANGS = ("it", "ru", "en", "uz")


def main() -> str:
    return (SRC / "main.ts").read_text(encoding="utf-8")


def i18n() -> str:
    return (SRC / "i18n.ts").read_text(encoding="utf-8")


def block(name: str) -> str:
    """The body of one function, ending at the next top-level one.

    Stops at `async function` as well as `function`. It did not, so slicing an async
    function ran on through every function after it until the next synchronous one — and an
    assertion that `remainingMs()` appeared "in the block" passed on a completely different
    function's use of it. Mutation caught that; nothing else would have.
    """
    text = main()
    start = text.index(f"function {name}(")
    end = len(text)
    for marker in ("\nfunction ", "\nasync function "):
        at = text.find(marker, start + 10)
        if at != -1:
            end = min(end, at)
    return text[start:end]


# --- 1. the answer buttons --------------------------------------------------

def test_the_answer_buttons_are_italian_in_every_language():
    """The words on the real exam paper. A candidate should meet them here first, not
    there."""
    text = i18n()
    assert text.count('vero: "VERO",') == 4, "vero is not VERO in all four locales"
    assert text.count('falso: "FALSO",') == 4, "falso is not FALSO in all four locales"
    for wrong in ("ВЕРНО", "НЕВЕРНО", "TO'G'RI", "NOTO'G'RI", 'vero: "TRUE"'):
        assert wrong not in text, f"a translated answer button survives: {wrong}"


# --- 2. turning them on -----------------------------------------------------

def test_turning_translations_on_warms_the_window_not_just_this_question():
    """One question at a time is what made it look like nothing happened."""
    toggle = main()[main().index("function translationToggle("):]
    toggle = toggle[:toggle.index("\nfunction ")]
    assert "warmTranslations()" in toggle, "the switch still only hydrates one question"

    warm = block("warmTranslations")
    assert "PREFETCH_WINDOW" in warm, "the warm-up must cover the window, not one question"
    assert "true" in warm.split("sessions.prefetch")[1][:80], (
        "the warm-up must WAIT — a fire-and-forget call raises the spinner and drops it "
        "again before anything has been fetched"
    )


def test_the_cached_off_state_is_cleared_before_refetching():
    """The server decides `translation_state` from the setting. Leaving every question at
    the `off` it was fetched with means the re-fetch has nothing to fill in."""
    warm = block("warmTranslations")
    assert '=== "off"' in warm and '"available"' in warm


def test_the_learner_is_told_the_translations_are_coming():
    """The wait is real — the translations genuinely do not exist yet — so the screen has to
    account for it rather than looking unresponsive."""
    warm = block("warmTranslations")
    assert "run.warming = true" in warm and "run.warming = false" in warm
    assert "fetching_translations" in block("runScreen")
    assert i18n().count("fetching_translations:") == 4


# --- 3. turning them off ----------------------------------------------------

def test_turning_translations_off_clears_the_one_on_screen():
    toggle = main()[main().index("function translationToggle("):]
    toggle = toggle[:toggle.index("\nfunction ")]
    assert "dropLoadedTranslations()" in toggle

    drop = block("dropLoadedTranslations")
    assert "for (const question of run.session.questions)" in drop, (
        "clearing only the current question leaves the rest of the paper carrying text the "
        "learner has switched off — the loop has to be over the whole paper"
    )
    assert 'translation_state = "off"' in drop
    assert "translation = null" in drop


# --- 4. the end of a completed paper ----------------------------------------

def test_a_finished_paper_offers_to_be_handed_in():
    run = block("runScreen")
    assert "const complete =" in run
    assert "run.answered.size >= run.session.question_count" in run
    assert 'mode === "exam"' in run.split("const complete =")[1][:120], (
        "practice has no fixed paper, so it can never be complete"
    )
    assert "finish_now" in run and "all_answered" in run


def test_handing_in_a_finished_paper_says_how_long_is_left():
    """The thing a candidate in a real exam room actually weighs. The default answer is to
    go back and check."""
    confirm = block("confirmHandIn")
    assert "remainingMs()" in confirm, "the confirmation must quote the real remaining time"
    assert "finish_confirm" in confirm
    assert "{time}" in i18n().split("finish_confirm:")[1][:200], (
        "the confirmation string has no slot for the time"
    )


def test_the_answer_sheet_is_still_reachable_from_the_finish_screen():
    """"No, I will re-check" has to lead somewhere — the sheet is how you get back to
    question 14."""
    run = block("runScreen")
    done = run[run.index("const complete ="):run.index("} else if (!answeredHere)")]
    assert "openAnswerSheet" in done


@pytest.mark.parametrize("key", ["all_answered", "finish_now", "finish_confirm",
                                 "fetching_translations"])
def test_every_language_has_the_new_strings(key):
    assert i18n().count(f"{key}:") == 4, f"{key} is not defined in all four locales"
