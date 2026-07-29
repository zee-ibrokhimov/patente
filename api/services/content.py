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
    explanation_access,
    translation_access,
)
from shared.constants import STATUS_APPROVED


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
    """Only ever an APPROVED explanation.

    A topic goes live when every explanation in it has been read by a human
    (plan §3.3). Drafts exist in the same table and must never be served — an
    absent explanation is acceptable, a confidently wrong one is not.
    """
    if cluster_id is None:
        return None
    return await session.scalar(
        select(Explanation).where(
            Explanation.cluster_id == cluster_id,
            Explanation.lang == lang,
            Explanation.status == STATUS_APPROVED,
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
    explanation = await get_explanation(session, question.cluster_id, user.lang)
    access = explanation_access(entitlement, explanation is not None)
    payload = {
        "explanation_state": access.value,
        "explanation": explanation.text if access is Access.SHOWN else None,
    }
    return payload, access
