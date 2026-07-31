"""Starting over.

Someone who has been drilling for a month, passed, and now wants to help a friend — or
who simply made a mess of their first fortnight and wants a clean readiness score —
currently has no way to do it. Their Leitner boxes, their exam history and their
vocabulary progress are permanent.

WHAT IS DESTROYED AND WHAT IS NOT

Destroyed: everything that describes how they are doing. Question progress, vocabulary
progress, and quiz sittings with their items.

Kept, deliberately:

  · the pass, and every Purchase row. Wiping progress must never cost someone money they
    have paid. This is the single most important line in this module.
  · language and the translations switch — settings, not progress.
  · channel membership, which is Telegram's fact and not ours to forget.
  · the event log, which is append-only and is how the owner sees what happened. Deleting
    from it to make a reset look tidy would corrupt the only honest record there is.

It is one transaction. A reset that removed the Leitner boxes and then failed before the
exam history would leave a learner with a readiness score computed from sittings whose
answers no longer exist.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Progress, QuizSession, QuizSessionItem, VocabProgress
from api.services import events
from shared.constants import EV_PROGRESS_RESET

log = logging.getLogger(__name__)


async def preview(session: AsyncSession, chat_id: int) -> dict:
    """What a reset would destroy, so the confirmation can be specific.

    "This will delete your progress" is a sentence people click past. "This will delete
    412 answers and 6 exams" is one they read.
    """
    answered = await session.scalar(
        select(func.coalesce(func.sum(Progress.seen), 0)).where(Progress.chat_id == chat_id)
    )
    questions = await session.scalar(
        select(func.count()).select_from(Progress).where(Progress.chat_id == chat_id)
    )
    sittings = await session.scalar(
        select(func.count()).select_from(QuizSession).where(QuizSession.chat_id == chat_id)
    )
    words = await session.scalar(
        select(func.count()).select_from(VocabProgress).where(VocabProgress.chat_id == chat_id)
    )
    return {
        "answers": int(answered or 0),
        "questions": int(questions or 0),
        "sittings": int(sittings or 0),
        "words": int(words or 0),
    }


async def reset_progress(session: AsyncSession, chat_id: int) -> dict:
    """Wipe everything that measures this learner. Returns what was removed.

    The pass is not touched. Neither is the purchase history, so a refund arriving after
    a reset still finds its row.
    """
    removed = await preview(session, chat_id)

    # Items first: they reference the sittings, and a database without ON DELETE CASCADE
    # configured for this pair would otherwise refuse.
    sitting_ids = select(QuizSession.id).where(QuizSession.chat_id == chat_id)
    await session.execute(
        delete(QuizSessionItem).where(QuizSessionItem.session_id.in_(sitting_ids))
    )
    await session.execute(delete(QuizSession).where(QuizSession.chat_id == chat_id))
    await session.execute(delete(Progress).where(Progress.chat_id == chat_id))
    await session.execute(delete(VocabProgress).where(VocabProgress.chat_id == chat_id))

    # Recorded rather than silent: a support message saying "my stats vanished" should be
    # answerable, and a reset is exactly the kind of thing someone forgets doing.
    await events.record(
        session, EV_PROGRESS_RESET, chat_id=chat_id,
        answers=removed["answers"], sittings=removed["sittings"], words=removed["words"],
    )
    await session.commit()
    log.info("progress reset for %s: %s", chat_id, removed)
    return removed
