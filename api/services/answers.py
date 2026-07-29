"""Recording an answer — the one write path that touches progress and entitlement.

This is also the conversion moment. A free user answers wrong, is told the answer
is FALSO, and wants to know why; that is exactly where the paywall belongs
(plan §4.3). So this function is where paywall_hit is logged and where a lifetime
taster explanation is spent — never in the bot, which would give two surfaces two
different definitions of "converted".
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, Question
from api.services import events
from api.services.content import explanation_payload
from api.services.entitlement import Access, Entitlement
from api.services.leitner import schedule
from shared.constants import (
    EV_ANSWER_GIVEN,
    EV_EXPLANATION_VIEWED,
    EV_PAYWALL_HIT,
    FIRST_BOX,
)


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

    payload, access = await explanation_payload(session, question, user, entitlement)

    if access is Access.SHOWN:
        if entitlement.spends_free_explanation:
            user.free_explanations_used += 1
            await session.flush()
        await events.record(
            session,
            EV_EXPLANATION_VIEWED,
            chat_id=user.chat_id,
            question_id=question.id,
            free_taster=entitlement.spends_free_explanation,
        )
    elif access is Access.LOCKED:
        await events.record(
            session,
            EV_PAYWALL_HIT,
            chat_id=user.chat_id,
            question_id=question.id,
            topic_id=question.topic_id,
            after_wrong_answer=not correct,
        )

    return {
        "question_id": question.id,
        "given": given,
        "correct": correct,
        "correct_answer": question.answer,
        "box": progress.box,
        "due_at": progress.due_at,
        "free_explanations_left": max(
            0, entitlement.free_explanations_left - (1 if access is Access.SHOWN
                                                     and entitlement.spends_free_explanation
                                                     else 0)
        ),
        **payload,
    }
