"""Assembling what a user is actually sent for a question.

Every payload that could carry a translation or an explanation is built here, so
there is exactly one place where entitlement is applied. Locked content is left
out of the response entirely rather than blanked — the client never receives text
it is not allowed to display.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Explanation, Figure, Question, Translation
from api.services.entitlement import (
    Access,
    Entitlement,
    explanation_offer,
    translation_access,
)
from shared.constants import SERVABLE_STATUSES


async def get_question(session: AsyncSession, question_id: int) -> Question | None:
    return await session.get(Question, question_id)


async def get_translation(
    session: AsyncSession, question_id: int, lang: str
) -> Translation | None:
    return await session.scalar(
        select(Translation).where(
            Translation.question_id == question_id, Translation.lang == lang
        )
    )


async def get_explanation(
    session: AsyncSession, cluster_id: int | None, lang: str
) -> Explanation | None:
    """An already-stored explanation that may be served. Never generates.

    Approved *or* draft, per STATUS.md §13. This used to be approved-only, on plan
    §3.3's rule that a topic ships when a human has read every explanation in it — but
    explanations are generated on request now, so the first reader of a draft is the
    user who asked for it. `SERVABLE_STATUSES` is where that line is drawn, and the
    automatic gates are what stands behind it: a flagged draft is withheld and reads as
    "nobody has written this", which is a state the paywall deliberately distinguishes
    from "pay for it".
    """
    if cluster_id is None:
        return None
    return await session.scalar(
        select(Explanation).where(
            Explanation.cluster_id == cluster_id,
            Explanation.lang == lang,
            Explanation.status.in_(SERVABLE_STATUSES),
        )
    )


async def question_payload(
    session: AsyncSession,
    question: Question,
    user,
    entitlement: Entitlement,
) -> dict:
    """The question as the user may see it. Italian is always present.

    The ministerial wording is the thing being learned, so it is never replaced
    by a translation — the translation rides underneath as a comprehension aid.
    """
    translation = await get_translation(session, question.id, user.lang)
    access = translation_access(entitlement, user, translation is not None)

    # Non-null once this figure has been uploaded to Telegram at least once; the
    # bot then re-sends by id instead of re-uploading the bytes (plan §6.4).
    file_id = None
    if question.image_path:
        figure = await session.get(Figure, question.image_path)
        file_id = figure.telegram_file_id if figure else None

    payload = {
        "id": question.id,
        "quesito_id": question.quesito_id,
        "topic_id": question.topic_id,
        "statement_it": question.statement_it,
        "stem_it": question.stem_it,
        "image": question.image_path,
        "image_file_id": file_id,
        "translation_state": access.value,
        "translation": None,
    }
    if access is Access.SHOWN and translation is not None:
        payload["translation"] = {
            "lang": translation.lang,
            "stem": translation.stem,
            "statement": translation.statement,
        }
    return payload


async def explanation_payload(
    session: AsyncSession, question: Question, user, entitlement: Entitlement
) -> tuple[dict, Access]:
    """What answering a question says about its explanation. Serves it if it is ready.

    Answering **never generates**. The explanation is warmed in the background when the
    question is served, so by the time the user answers it is normally cached and comes
    back with the verdict — no wait, and no call for the many users who answer and move
    on without caring why.

    When warming has not finished, or failed, or nobody has run it, the answer reports
    `available` instead and the client offers a button that does generate. That is the
    fallback rather than the usual path, which is why it is worth having both.
    """
    if question.cluster_id is None:
        return {"explanation_state": Access.UNAVAILABLE.value, "explanation": None}, \
            Access.UNAVAILABLE

    stored = await session.scalar(
        select(Explanation).where(
            Explanation.cluster_id == question.cluster_id,
            Explanation.lang == user.lang,
        )
    )
    ready = stored is not None and stored.status in SERVABLE_STATUSES

    if ready and entitlement.can_explain:
        # Spending the taster and logging the view stay in `explanations.deliver`, which
        # the route calls for this case too — this function only reads.
        return {"explanation_state": Access.SHOWN.value, "explanation": stored.text}, \
            Access.SHOWN

    access = explanation_offer(
        entitlement,
        groundable=True,
        # A stored row that failed a gate is the one case where we know in advance that
        # nothing servable will come back without a human, so no button is offered.
        withheld=stored is not None and not ready,
    )
    return {"explanation_state": access.value, "explanation": None}, access
