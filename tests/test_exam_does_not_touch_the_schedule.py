"""An exam measures. It must not teach, and it must not reorder the practice queue.

`MODE_UPDATES_SCHEDULE[MODE_EXAM] = False` was honoured on the UPDATE branch of
`record_answer` and ignored on the CREATE branch above it. Most of a 30-question random
draw from 7106 is questions the learner has never seen, so every exam created thirty rows
at box 1 with `due_at` = the exam's own clock — and `selection.practice_paper` takes
`due_at <= now ORDER BY due_at` first.

The result is verbatim what the constant's own comment (shared/constants.py) says it exists
to prevent: "would re-stamp thirty due_at values to the exam's clock — which then dominates
the practice queue, because selection orders strictly by due_at."

Reproduced by the audit: a learner practises ten questions correctly, sits an exam,
answers ALL THIRTY correctly — and the very next practice batch hands back 27 of the exam's
own questions, in exam order, ahead of their real backlog. Including every one they got
right, ranked as "just got it wrong", because a correct and an incorrect exam answer
produce identical box-1/due-now rows.

The existing regression test asserted only `progress.box == 1`, which a freshly created row
satisfies. It never looked at `due_at`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from api.models import Progress, Question, User
from api.services import answers, selection
from api.services.entitlement import Entitlement

NOW = datetime.now(timezone.utc)


@pytest.fixture
async def bank(api_db):
    """A question bank big enough for the selection tiers to actually compete.

    The shared fixture has FOUR questions. The first version of the two queue tests below
    used it, and both passed against the bug — with a 30-question batch drawn from a pool
    of four, every tier returns the same rows and the assertions were vacuous. The mutation
    run is what exposed it: breaking the guard failed only 2 of 6 tests.
    """
    async with api_db() as s:
        s.add_all([
            Question(id=1000 + i, quesito_id=200, topic_id=2, cluster_id=None,
                     statement_it=f"Affermazione di prova numero {i}",
                     answer=(i % 2 == 0), source_version="v1")
            for i in range(60)
        ])
        await s.commit()
        return [q for q in (await s.scalars(
            select(Question.id).where(Question.id >= 1000).order_by(Question.id))).all()]


def _ent() -> Entitlement:
    return Entitlement(has_pass=True, pass_expires_at=NOW + timedelta(days=30),
                       free_explanations_left=0)


async def _answer(api_db, chat_id, question_id, *, correct, update_schedule, now=None):
    async with api_db() as s:
        user = await s.get(User, chat_id)
        q = await s.get(Question, question_id)
        await answers.record_answer(
            s, user, q, given=(q.answer if correct else not q.answer),
            entitlement=_ent(), update_schedule=update_schedule,
            offer_explanation=False, now=now or NOW,
        )
        await s.commit()


async def _progress(api_db, chat_id, question_id) -> Progress | None:
    async with api_db() as s:
        return await s.get(Progress, (chat_id, question_id))


# --- the row an exam creates ------------------------------------------------

async def test_an_exam_answer_does_not_make_a_new_question_due(api_db, registered):
    """THE bug. `due_at = now` on a brand-new row puts it at the head of the due tier."""
    await _answer(api_db, 42, 1, correct=True, update_schedule=False)
    row = await _progress(api_db, 42, 1)
    assert row is not None, "the counters still need a row"
    assert row.due_at > NOW + timedelta(days=1), \
        f"an exam scheduled a question for {row.due_at} — it is now at the head of practice"


async def test_an_exam_answer_still_counts_as_seen(api_db, registered):
    """The row exists for `seen`/`wrong`, which feed /stats and the reset confirmation.
    Dropping it would make stats lie, which is why the row is created at all."""
    await _answer(api_db, 42, 1, correct=False, update_schedule=False)
    row = await _progress(api_db, 42, 1)
    assert row.seen == 1
    assert row.wrong == 1


async def test_a_practice_answer_is_still_scheduled_normally(api_db, registered):
    """The guard is conditional on the mode, not a blanket change."""
    await _answer(api_db, 42, 1, correct=False, update_schedule=True)
    row = await _progress(api_db, 42, 1)
    assert row.due_at < NOW + timedelta(hours=1), \
        "a wrong practice answer must come back within the session"


async def test_studying_a_question_an_exam_touched_restores_its_schedule(api_db, registered):
    """Not permanent. The first practice answer runs the Leitner scheduler and the
    question rejoins the queue on its real merits."""
    await _answer(api_db, 42, 1, correct=True, update_schedule=False)
    parked = (await _progress(api_db, 42, 1)).due_at

    await _answer(api_db, 42, 1, correct=False, update_schedule=True)
    row = await _progress(api_db, 42, 1)
    assert row.due_at < parked
    assert row.due_at < NOW + timedelta(hours=1)


# --- what the learner actually experiences ----------------------------------

async def test_practice_after_an_exam_is_not_a_replay_of_the_exam(api_db, registered, bank):
    """The user-visible failure: finish a mock exam, tap Practice to study, and get the
    same thirty questions back — including every one you got RIGHT, ranked as "just got it
    wrong", because a correct and an incorrect exam answer produce identical rows."""
    sat = bank[:30]
    for qid in sat:
        await _answer(api_db, 42, qid, correct=True, update_schedule=False)

    async with api_db() as s:
        user = await s.get(User, 42)
        batch = await selection.practice_paper(s, user, _ent(), count=30, now=NOW)

    overlap = len({q.id for q in batch} & set(sat))
    assert overlap == 0, (
        f"{overlap}/30 of the practice batch was the exam the learner just sat — "
        f"there are {len(bank) - 30} untouched questions it should have drawn instead")


async def test_the_real_backlog_still_comes_first(api_db, registered, bank):
    """The positive half: what they got wrong in PRACTICE must outrank anything an exam
    touched. Asserting POSITION rather than membership — with a draw this size membership
    passes by luck often enough to hide a regression, which this suite has been caught by
    before."""
    backlog, sat = bank[:5], bank[5:35]

    for qid in backlog:                       # got these wrong while studying
        await _answer(api_db, 42, qid, correct=False, update_schedule=True,
                      now=NOW - timedelta(hours=2))
    for qid in sat:                           # then sat an exam
        await _answer(api_db, 42, qid, correct=True, update_schedule=False)

    async with api_db() as s:
        user = await s.get(User, 42)
        batch = await selection.practice_paper(s, user, _ent(), count=30, now=NOW)

    head = [q.id for q in batch[:5]]
    assert set(head) == set(backlog), \
        f"the practice queue opened with {head}, not the learner's own mistakes {backlog}"
