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

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question
from api.services import events, explanations
from api.services.entitlement import Entitlement, evaluate
from api.services.leitner import schedule
from shared.constants import EV_ANSWER_GIVEN, FIRST_BOX


async def record_answer(
    session: AsyncSession,
    user,
    question: Question,
    given: bool,
    entitlement: Entitlement,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    correct = given == question.answer

    progress = await session.get(Progress, (user.chat_id, question.id))
    if progress is None:
        progress = Progress(
            chat_id=user.chat_id, question_id=question.id, box=FIRST_BOX, due_at=now
        )
        session.add(progress)
        # Column defaults are applied at INSERT, not at construction — without
        # this flush `seen` and `wrong` are still None when incremented below.
        await session.flush()

    if entitlement.can_use_spaced_repetition:
        progress.box, progress.due_at = schedule(progress.box, correct, now)
    else:
        progress.due_at = now
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
        # Wrong answers before purchase is the core conversion metric (§4.3), and
        # it is only reconstructable if entitlement is stamped on the event.
        has_pass=entitlement.has_pass,
    )

    # Serves the explanation if warming already produced it, and never generates one
    # here: paying for a call at this moment would charge for every user who answers and
    # moves on. `generate_if_missing=False` is that rule, and the client gets an
    # `available` offer to fall back on when warming has not landed.
    payload, access = await explanations.deliver(
        session, question, user, entitlement, generate_if_missing=False
    )

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
