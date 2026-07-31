"""Recording an answer — the one write path that touches progress and entitlement.

A free user answers wrong, is told the answer is FALSO, and wants to know why: that is
where the paywall belongs (plan §4.3). It used to be logged here, because the
explanation was returned inline with the answer. Explanations are produced on request
now (STATUS.md §13), so *wanting to know why* is a distinct action — a button the user
taps — and the paywall, the taster spend and the view event all moved to
`explanations.deliver`, where they actually happen. This function reports only whether
an explanation can be offered.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question
from api.services import events, explanations
from api.services.entitlement import Entitlement, evaluate
from api.services.leitner import schedule
from shared.constants import EV_ANSWER_GIVEN, FIRST_BOX

# How far out a row goes when the answer that created it scheduled nothing — an exam.
#
# `progress.due_at` is NOT NULL, so there is no "seen but unscheduled" to write. A date
# this far out is never reached by the due tier of `selection.practice_paper` and sorts to
# the back of its not-yet-due tier, which is the correct place for material the learner has
# been TESTED on and has not STUDIED. It is not permanent: the first practice answer runs
# `schedule(box, correct, now)` and the question rejoins the queue on its real merits.
UNSCHEDULED = timedelta(days=3650)


async def record_answer(
    session: AsyncSession,
    user,
    question: Question,
    given: bool,
    entitlement: Entitlement,
    now: datetime | None = None,
    *,
    update_schedule: bool = True,
    offer_explanation: bool = True,
) -> dict:
    """Record one answer.

    The two keyword flags exist because this function fused three things that an exam
    needs separated, and shipping exam mode without them would have been wrong in ways
    that are invisible until much later:

      · `update_schedule=False` keeps a timed exam out of the Leitner scheduler. Exam
        answers are unassisted, pressured guesses; letting them promote a question into
        the 7- or 30-day box means the user cannot answer what is scheduled, and the
        thirty re-stamped `due_at` values then dominate the practice queue because
        selection orders strictly by due date. The damage is silent and only surfaces
        weeks later, by which time nothing distinguishes an exam-written Progress row
        from a practice-written one.
      · `offer_explanation=False` keeps it out of `explanations.deliver`, which is the
        ONE place a free taster is spent and the ONE place EV_PAYWALL_HIT is written.
        An exam that shows no explanations would otherwise burn all three of a user's
        lifetime tasters on text never rendered, and log up to thirty paywall hits per
        sitting for a paywall nobody saw — into an append-only table that cannot be
        cleaned, corrupting the one conversion metric §4.3 prices on.

    Counters (`seen`, `wrong`) still move in both modes: the user did see the question
    and did answer it, and stats would lie otherwise.
    """
    now = now or datetime.now(timezone.utc)
    correct = given == question.answer

    progress = await session.get(Progress, (user.chat_id, question.id))
    if progress is None:
        # AN EXAM MUST NOT MAKE A QUESTION DUE — not even a question it is meeting for the
        # first time.
        #
        # `update_schedule` was honoured on the update branch below and ignored here, and
        # most of a 30-question random draw from 7106 is questions the learner has never
        # seen. So every exam created thirty rows at box 1 with `due_at` = the exam's
        # clock, and `selection.practice_paper` takes `due_at <= now ORDER BY due_at`
        # FIRST — putting the exam at the head of the practice queue and keeping it there,
        # ahead of the learner's real backlog, until practised.
        #
        # Verbatim the failure MODE_UPDATES_SCHEDULE exists to prevent, and the comment
        # fourteen lines below says so: "would re-stamp thirty due_at values to the exam's
        # clock — which then dominates the practice queue". Reproduced: answer all 30
        # correctly, and the next practice batch is 27 of the exam's own questions, in exam
        # order — including every one they got RIGHT, ranked as "just got it wrong",
        # because right and wrong exam answers produce identical box-1 due-now rows.
        #
        # The row still has to exist: `seen` and `wrong` feed /stats and the reset
        # confirmation, and the docstring above is right that stats would lie without it.
        # `due_at` is NOT NULL, so "this answer scheduled nothing" is expressed as a date
        # only real study can bring forward — the first practice answer runs
        # `schedule(box, correct, now)` and the question rejoins the queue normally.
        progress = Progress(
            chat_id=user.chat_id,
            question_id=question.id,
            box=FIRST_BOX,
            due_at=now if update_schedule else now + UNSCHEDULED,
        )
        session.add(progress)
        # Column defaults are applied at INSERT, not at construction — without
        # this flush `seen` and `wrong` are still None when incremented below.
        await session.flush()

    if update_schedule and entitlement.can_use_spaced_repetition:
        progress.box, progress.due_at = schedule(progress.box, correct, now)
    elif update_schedule:
        progress.due_at = now
    # When update_schedule is False, box and due_at are left exactly as they were. Not
    # even `due_at = now`, which would front-load the practice queue with whatever the
    # exam happened to touch.
    progress.seen += 1
    progress.wrong += 0 if correct else 1
    progress.last_answer_at = now
    await session.flush()

    await events.record(
        session,
        EV_ANSWER_GIVEN,
        chat_id=user.chat_id,
        question_id=question.id,
        topic_id=question.topic_id,
        correct=correct,
        box=progress.box,
        # So an exam answer is separable from a studied one in the funnel forever.
        # Events cannot be backfilled, so this has to be stamped at write time.
        graded=update_schedule,
        # Wrong answers before purchase is the core conversion metric (§4.3), and
        # it is only reconstructable if entitlement is stamped on the event.
        has_pass=entitlement.has_pass,
    )

    # Serves the explanation if warming already produced it, and never generates one
    # here: paying for a call at this moment would charge for every user who answers and
    # moves on. `generate_if_missing=False` is that rule, and the client gets an
    # `available` offer to fall back on when warming has not landed.
    if offer_explanation:
        payload, _access = await explanations.deliver(
            session, question, user, entitlement, generate_if_missing=False
        )
    else:
        payload = {}

    return {
        "question_id": question.id,
        "given": given,
        "correct": correct,
        "correct_answer": question.answer,
        "box": progress.box,
        "due_at": progress.due_at,
        # Read after `deliver`, which is what spends it.
        "free_explanations_left": evaluate(user).free_explanations_left,
        **payload,
    }
