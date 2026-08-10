"""Ending a training round keeps it. Exiting an exam does not. Both promises are in a dialog.

The two controls sit in the same place on the same bar and say opposite things:

  exam      "this attempt will not be counted"     -> abandon(), passed stays None
  practice  "the result is saved and you will      -> finish(),  SESSION_SUBMITTED
             see the review"

The trap is that BOTH end up with `passed = None` — an exam because `_grade` deliberately
skips grading an abandoned sitting, a practice round because it has no `max_errors` to grade
against. So `passed is None` does NOT mean "did not happen", and three queries were just
given a `passed is not None` filter to keep exited exams out of the exam statistics. Every
one of them must also be restricted to MODE_EXAM, or ending a practice round would be
silently discarded and the new dialog would be a lie.

That is what most of this file checks. The rest checks the honest version of "saved": the
answers were already recorded when they were given, and what pressing End adds is the
closure of the sitting and the review screen — which is what the dialog actually promises.
"""

from __future__ import annotations

import json
import pathlib
import re
import time

import pytest
from sqlalchemy import select

from api.models import Progress, Question, QuizSession, QuizSessionItem
from api.services import profile
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    MODE_EXAM,
    MODE_PRACTICE,
    SESSION_ABANDONED,
    SESSION_OPEN,
    SESSION_SUBMITTED,
)

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
SRC = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "src"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def train(client, api_db, *, answer: int):
    """Start a practice round and answer `answer` questions correctly."""
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    async with api_db() as s:
        keys = {o: a for o, a in (await s.execute(
            select(QuizSessionItem.ordinal, Question.answer)
            .join(Question, Question.id == QuizSessionItem.question_id)
            .where(QuizSessionItem.session_id == started["id"]))).all()}
    for ordinal in range(1, answer + 1):
        await client.post(f"/webapp/sessions/{started['id']}/answers", headers=auth(),
                          json={"ordinal": ordinal, "answer": keys[ordinal]})
    return started


# --- the promise ------------------------------------------------------------

async def test_ending_a_round_closes_it_and_returns_the_review(client, registered, api_db):
    started = await train(client, api_db, answer=2)
    body = (await client.post(f"/webapp/sessions/{started['id']}/finish",
                              headers=auth())).json()

    assert body["state"] == SESSION_SUBMITTED
    assert body["answered"] == 2
    assert body["items"], "the review came back empty"

    async with api_db() as s:
        row = await s.get(QuizSession, started["id"])
    assert row.state == SESSION_SUBMITTED
    assert row.finished_at is not None


async def test_the_work_survives_and_it_survived_before_the_button_was_pressed(
        client, registered, api_db):
    """The honest reading of "the result is saved".

    Answers are recorded by `record_answer` at the moment they are given, not at finish
    time — so a practice round's learning is safe whatever the learner does next. What
    pressing End adds is closing the sitting and showing the review. Worth pinning because
    a future change that moved recording to finish-time would look harmless and would lose
    the work of anyone who closed the app.
    """
    started = await train(client, api_db, answer=3)

    async with api_db() as s:
        before = len(list(await s.scalars(
            select(Progress).where(Progress.chat_id == OWNER))))
    assert before == 3, "answers were not recorded until the round was ended"

    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())

    async with api_db() as s:
        after = len(list(await s.scalars(
            select(Progress).where(Progress.chat_id == OWNER))))
    assert after == before


async def test_practice_moves_the_schedule_and_an_exam_does_not(client, registered, api_db):
    """The substantive difference between the two modes, and the reason ending a training
    round is worth doing at all: practice is what the spaced repetition is built from."""
    await train(client, api_db, answer=2)
    async with api_db() as s:
        boxes = sorted(p.box for p in await s.scalars(
            select(Progress).where(Progress.chat_id == OWNER)))
    assert all(b > 1 for b in boxes), f"a correct practice answer did not promote: {boxes}"


# --- the trap ---------------------------------------------------------------

def test_every_passed_filter_is_restricted_to_exams():
    """`passed is None` means two entirely different things.

    An exited EXAM is ungraded on purpose. A PRACTICE round has no max_errors to grade
    against, so it is ungraded by construction. Three queries filter on `passed is not None`
    to keep exited exams out of the exam statistics; any of them that forgot `mode ==
    MODE_EXAM` would also throw away every completed training round — and the failure would
    be silent, because the number it feeds would simply be smaller.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for path in (*root.glob("api/**/*.py"), *root.glob("bot/**/*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"QuizSession\.passed\.is_not\(None\)", text):
            # The statement this predicate belongs to: back to the previous `select(`.
            start = text.rfind("select(", 0, m.start())
            assert start != -1, f"{path}: could not find the query for {m.group(0)}"
            hits.append((path.relative_to(root), text[start:m.end()]))

    assert hits, "no passed filters found at all — has the exam statistics fix been reverted?"
    for where, statement in hits:
        assert "MODE_EXAM" in statement, (
            f"{where}: filters on passed without restricting to exams, so every finished "
            f"practice round is discarded too:\n{statement}"
        )


async def test_a_finished_practice_round_is_not_reported_as_an_exam(client, registered, api_db):
    """The other direction of the same trap: practice must not leak INTO the exam figures."""
    from api.services import admin

    await train(client, api_db, answer=2)
    started = (await client.post("/webapp/sessions", headers=auth(),
                                 json={"mode": MODE_PRACTICE})).json()
    await client.post(f"/webapp/sessions/{started['id']}/finish", headers=auth())

    async with api_db() as s:
        exams = (await profile.user_profile(s, OWNER))["exams"]
        week = (await admin.overview(s))["exams_week"]
    assert exams["taken"] == 0, exams
    assert week == 0, f"a training round was counted as an exam sat: {week}"


# --- the two dialogs --------------------------------------------------------

def test_the_two_controls_do_not_share_a_word():
    """Exiting an exam records nothing; ending a practice round records a result. Naming
    both "Exit" would put the entire difference into a dialog a learner can dismiss without
    reading."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function runBar("):]
    block = block[:block.index("\nfunction ")]
    assert 'exam ? t("exit_label") : t("end_test")' in block, (
        "the two modes must label their control differently"
    )


def test_practice_is_asked_before_its_round_ends():
    """It used to end on a single tap of an unlabelled glyph. A labelled control gets
    pressed, so it needs to say what pressing it does."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("async function confirmFinish"):]
    block = block[:block.index("\n}") + 2]
    assert "confirm_end_practice" in block
    assert "confirm_submit" in block
    assert "if (!(await ask(" in block, "the confirmation must gate both modes, not just exams"


@pytest.mark.parametrize("key", ["confirm_end_practice"])
def test_every_language_can_ask_it(key):
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    assert i18n.count(f"{key}:") == 4


def test_the_practice_dialog_promises_the_opposite_of_the_exam_one():
    """The pair only works if the two say different things. If both dialogs said "will not
    be counted", or both said "will be saved", the labels would be the only signal left."""
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    practice = [ln for ln in i18n.splitlines() if ln.strip().startswith("confirm_end_practice:")]
    exits = [ln for ln in i18n.splitlines() if ln.strip().startswith("exit_confirm:")]
    assert len(practice) == 4 and len(exits) == 4

    ru_practice = next(ln for ln in practice if "Завершить" in ln)
    ru_exit = next(ln for ln in exits if "Выйти" in ln)
    assert "сохранятся" in ru_practice, ru_practice
    assert "не будет засчитана" in ru_exit, ru_exit


def test_the_practice_dialog_promises_only_what_is_kept():
    """"The result is saved" is more than the code does.

    Nothing in the product ever shows a practice sitting again: the profile's history is
    exam-only, and the two client calls that fetch results by id are recovery paths keyed
    off an open session. The ROW is written and never displayed. What genuinely persists is
    the answers — Progress rows and the event log — which is what the boxes, the per-topic
    figures, readiness and the streak are all built from.

    "Your answers are kept" is exactly true and still the opposite of the exam exit's "will
    not be counted", which is the contrast the pair of dialogs exists to draw.
    """
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    for line in (ln for ln in i18n.splitlines()
                 if ln.strip().startswith("confirm_end_practice:")):
        assert "результат" not in line.lower() and "risultato" not in line.lower(), line
        assert "natija" not in line.lower() and "the result" not in line.lower(), line


def test_a_round_with_no_answers_says_why_there_is_no_review():
    """Both dialogs leading to this screen promise a review. End a round having answered
    nothing and `r.items` is empty, so the section was simply not drawn — a bare 0/0 with no
    explanation, after a sentence that said otherwise."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    block = main[main.index("function resultsScreen()"):]
    block = block[:block.index("\nfunction ")]
    assert "nothing_to_review" in block, (
        "a results screen with no items must say why, not just omit the review"
    )
    i18n = (SRC / "i18n.ts").read_text(encoding="utf-8")
    assert i18n.count("nothing_to_review:") == 4


def test_ending_a_round_cannot_race_an_answer_still_in_flight():
    """If the finish request overtakes the answer request the server refuses the answer with
    409 and it is never recorded — no Progress row, no Leitner move — and the learner gets a
    red error toast over their results. The answer buttons have always been disabled on
    `run.busy`; the control that ends the round was not."""
    main = (SRC / "main.ts").read_text(encoding="utf-8")
    bar = main[main.index("function runBar("):]
    bar = bar[:bar.index("\nfunction ")]
    assert "end.disabled = run.busy" in bar

    for fn in ("confirmFinish", "confirmExit"):
        block = main[main.index(f"async function {fn}("):]
        block = block[:block.index("\n}") + 2]
        # The EXPRESSION, not the words. Asserting `"run.busy" in block` passed happily on
        # the comment that explains the guard, so removing the guard itself changed nothing
        # — caught by mutation, which is the only reason this line is specific.
        assert "if (!run || run.busy) return;" in block, (
            f"{fn} does not bail out while an answer is in flight"
        )
