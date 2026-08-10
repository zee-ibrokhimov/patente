"""Assembling the question payload.

Locked content is left out of the response entirely rather than blanked — the client
never receives text it is not allowed to display (plan §6.3).

Explanations are **not** built here. They used to be, but they are generated on request
now and the decision about what may be served got genuinely intricate: status, per-
statement disputes, entitlement, whether to pay for a cache miss. Two copies of that
would disagree within a month, so it lives once, in `api/services/explanations.py`.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Figure, Question, Translation
from api.services.entitlement import Access, Entitlement, translation_offer


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
    from api.services.translations import reading_language
    translation = await get_translation(session, question.id, reading_language(user))
    # `AVAILABLE` when nothing is stored yet: the client fetches it and edits the message,
    # rather than the question waiting on a translation call.
    access = translation_offer(entitlement, user, translation is not None)

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
