"""Exam and practice sittings.

An exam is a measurement, so three things have to be true and none of them can be left
to the client:

  · **The clock is the server's.** The Mini App's only identity is an initData blob;
    there is no session cookie to bind a countdown to, and a client-held deadline is
    editable by anyone who can open devtools. `expires_at` is computed here at creation
    and enforced on every subsequent touch.
  · **Nothing is revealed until the end.** Not the verdict on an answer, not the running
    error count. The answer path returns a different shape in exam mode, and history
    excludes the sitting that is still open.
  · **The paper is frozen.** See `QuizSession`'s docstring for why that single decision
    buys no-repeats, resumability, server-side grading and a one-round-trip start.

Practice is the same machinery with the clock and the secrecy removed.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Question, QuizSession, QuizSessionItem, Translation
from api.services import events, selection
from api.services.entitlement import evaluate
from shared.constants import (
    EV_EXAM_FINISHED,
    EV_EXAM_STARTED,
    EXAM_MAX_ERRORS,
    EXAM_MINUTES,
    EXAM_QUESTIONS,
    PRACTICE_BATCH,
    MODE_EXAM,
    REPEAT_SMART,
    REPEAT_SOURCES,
    MODE_PRACTICE,
    TRANSLATION_LANGUAGES,
    SESSION_ABANDONED,
    SESSION_EXPIRED,
    SESSION_OPEN,
    SESSION_SUBMITTED,
)

log = logging.getLogger(__name__)


class SessionError(Exception):
    """Raised for anything a route should turn into a 4xx. Services do not import
    HTTPException — routes own the HTTP vocabulary."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


async def _open_sessions(session: AsyncSession, chat_id: int) -> list[QuizSession]:
    rows = await session.scalars(
        select(QuizSession)
        .where(QuizSession.chat_id == chat_id, QuizSession.state == SESSION_OPEN)
        .order_by(QuizSession.started_at)
    )
    return list(rows)


async def enforce_deadline(
    session: AsyncSession, row: QuizSession, now: datetime | None = None
) -> QuizSession:
    """Grade an exam whose time ran out, wherever we happen to notice.

    There is no scheduler here, so "the clock ran out" is discovered on the next touch —
    which may be minutes or hours later, because the user closed Telegram. The deadline
    is still authoritative: the grade uses `expires_at`, not the moment of discovery, so
    an answer that arrived late cannot count and a user who reopens tomorrow gets the
    result they actually earned rather than an abandoned session.
    """
    now = now or datetime.now(timezone.utc)
    if row.state != SESSION_OPEN or row.expires_at is None or now < row.expires_at:
        return row
    await _grade(session, row, state=SESSION_EXPIRED, finished_at=row.expires_at)
    return row


async def create(
    session: AsyncSession,
    user,
    mode: str,
    now: datetime | None = None,
    source: str = REPEAT_SMART,
) -> tuple[QuizSession, list]:
    """Start a sitting and return it with its whole paper.

    Deadlines are enforced BEFORE the abandon sweep, and the order matters: a user whose
    twenty minutes elapsed while Telegram was backgrounded, who reopens and taps Start,
    must get their previous exam graded rather than silently abandoned. Sweeping first
    would throw away a completed result.
    """
    now = now or datetime.now(timezone.utc)

    for existing in await _open_sessions(session, user.chat_id):
        await enforce_deadline(session, existing, now)
        if existing.state == SESSION_OPEN:
            # Still genuinely open — starting a new sitting abandons it. Kept as a row
            # rather than deleted: EV_SESSION_START already names this id, and the event
            # log is append-only.
            await _grade(session, existing, state=SESSION_ABANDONED, finished_at=now)

    is_exam = mode == MODE_EXAM
    count = EXAM_QUESTIONS if is_exam else PRACTICE_BATCH
    # The exam is a uniform random draw because that is what an exam IS. Practice is
    # teaching, so it serves what is due first — see selection.practice_paper for the
    # bug that made this necessary.
    #
    # `source` only ever changes practice, and only the DRAW. A repeat round grades answers
    # and moves the Leitner schedule exactly like any other practice, because the learner is
    # genuinely studying — see selection.repeat_paper. An exam ignores it outright: a
    # simulator drawn from your own mistakes reports a score that means nothing.
    if is_exam:
        paper = await selection.exam_paper(session, count)
    elif source == REPEAT_SMART:
        paper = await selection.practice_paper(session, user, evaluate(user), count)
    else:
        paper = await selection.repeat_paper(session, user, source, count)

    if not paper:
        # A repeat round with nothing to repeat is a normal, explainable state — a learner
        # who has never got one wrong asking for their mistakes — and it needs a different
        # answer from "the question bank is missing", which is a 503 about the server.
        if not is_exam and source != REPEAT_SMART:
            raise SessionError("nothing to repeat yet", status=409)
        raise SessionError("no questions available", status=503)

    row = QuizSession(
        chat_id=user.chat_id,
        mode=mode,
        state=SESSION_OPEN,
        started_at=now,
        expires_at=now + timedelta(minutes=EXAM_MINUTES) if is_exam else None,
        question_count=len(paper),
        # Copied onto the row, not read from constants at grading time: plan §11 leaves
        # the format open, and changing it later must not re-grade a past sitting.
        max_errors=EXAM_MAX_ERRORS if is_exam else None,
        answered=0,
        wrong=0,
    )
    session.add(row)
    await session.flush()

    for ordinal, question in enumerate(paper, start=1):
        session.add(
            QuizSessionItem(
                session_id=row.id, ordinal=ordinal, question_id=question.id
            )
        )
    await session.flush()

    if is_exam:
        await events.record(
            session, EV_EXAM_STARTED, chat_id=user.chat_id,
            session_id=row.id, questions=len(paper), minutes=EXAM_MINUTES,
        )
    return row, paper


async def load_owned(
    session: AsyncSession, user, session_id: int, now: datetime | None = None
) -> QuizSession:
    """Fetch a sitting that belongs to this caller, or refuse.

    404 rather than 403 for someone else's session: confirming that an id exists is
    itself a leak, and the Mini App is a public surface.
    """
    row = await session.get(QuizSession, session_id)
    if row is None or row.chat_id != user.chat_id:
        raise SessionError("unknown session", status=404)
    await enforce_deadline(session, row, now)
    return row


async def answer(
    session: AsyncSession,
    user,
    row: QuizSession,
    ordinal: int,
    given: bool,
    entitlement,
    now: datetime | None = None,
) -> dict:
    """Answer one item of the paper.

    Write-once per ordinal. A timed exam on a flaky mobile connection will retry, and
    `record_answer` is not idempotent — a re-POST would double-count `seen`/`wrong` and
    re-run the scheduler. Re-answering is simply refused rather than silently applied.
    """
    from api.services.answers import record_answer
    from shared.constants import MODE_OFFERS_EXPLANATION, MODE_UPDATES_SCHEDULE

    now = now or datetime.now(timezone.utc)
    if row.state != SESSION_OPEN:
        raise SessionError("this session is already finished", status=409)

    item = await session.get(QuizSessionItem, (row.id, ordinal))
    if item is None:
        raise SessionError("no such question in this session", status=404)
    if item.given is not None:
        raise SessionError("already answered", status=409)

    from api.models import Question

    question = await session.get(Question, item.question_id)
    outcome = await record_answer(
        session, user, question, given, entitlement, now,
        update_schedule=MODE_UPDATES_SCHEDULE[row.mode],
        offer_explanation=MODE_OFFERS_EXPLANATION[row.mode],
    )

    item.given = given
    item.correct = outcome["correct"]
    item.answered_at = now
    row.answered += 1
    row.wrong += 0 if outcome["correct"] else 1
    await session.flush()

    if row.mode == MODE_EXAM:
        # Exam mode reveals NOTHING per answer — not the verdict, not the running error
        # count. Building the response from a whitelist rather than deleting keys from
        # `outcome` means a field added to record_answer later cannot leak by default.
        return {"session_id": row.id, "ordinal": ordinal, "answered": row.answered,
                "remaining": row.question_count - row.answered}
    return {"session_id": row.id, "ordinal": ordinal, "answered": row.answered,
            "remaining": row.question_count - row.answered, **outcome}


async def extend(
    session: AsyncSession, user, row: QuizSession, now: datetime | None = None
) -> list:
    """Add another batch to a practice sitting that has run out.

    Practice is unlimited by design — it ends when the learner says so, not when a
    counter runs out. Only the transport is batched.

    Refused for an exam, and not merely by convention: an exam is thirty questions
    because the real one is, and a simulator whose length the client can extend reports
    a score that means nothing. Refused for a closed sitting too, so a finished practice
    run cannot be reopened and have its result quietly changed.
    """
    now = now or datetime.now(timezone.utc)
    if row.mode != MODE_PRACTICE:
        raise SessionError("only a practice sitting can be extended", status=409)
    if row.state != SESSION_OPEN:
        raise SessionError("this sitting is closed", status=409)

    used = set((await session.scalars(
        select(QuizSessionItem.question_id).where(QuizSessionItem.session_id == row.id)
    )).all())
    more = await selection.practice_paper(
        session, user, evaluate(user), PRACTICE_BATCH, exclude=used
    )
    if not more:
        # The learner has worked through all 7106. Not an error: they have simply
        # finished the bank, and the sitting stays open so they can end it themselves.
        return []

    for offset, question in enumerate(more, start=1):
        session.add(QuizSessionItem(
            session_id=row.id, ordinal=row.question_count + offset,
            question_id=question.id,
        ))
    row.question_count += len(more)
    await session.flush()
    return more


async def cluster_at(
    session: AsyncSession, row: QuizSession, ordinal: int
) -> int | None:
    """The cluster of the item at `ordinal`, or None if the paper does not go that far.

    Exists so the route can warm an explanation a few questions ahead of where the learner
    is, without the route needing to know how a sitting stores its paper.
    """
    return await session.scalar(
        select(Question.cluster_id)
        .join(QuizSessionItem, QuizSessionItem.question_id == Question.id)
        .where(QuizSessionItem.session_id == row.id,
               QuizSessionItem.ordinal == ordinal)
    )


async def _grade(
    session: AsyncSession,
    row: QuizSession,
    state: str,
    finished_at: datetime,
) -> None:
    row.state = state
    row.finished_at = finished_at
    if row.max_errors is not None and state != SESSION_ABANDONED:
        # Unanswered questions count against you, exactly as in the real exam: running
        # out of time is a way to fail, not a way to defer. That is why EXPIRED is graded.
        #
        # ABANDONED is not, and the difference is the whole point. Abandoning is what
        # happens when the learner starts anything else — `create` sweeps any open sitting,
        # of ANY mode, so tapping Practice mid-exam lands here. Grading that produced
        # `passed = False` (answered != question_count), and the profile then filed it as a
        # sat exam: "Exams taken 2 · Passed 1", with a history row reading FAILED 0/30 for
        # a sitting the learner never took — a row saying they got nothing wrong and still
        # failed. The pass rate fell while avg_errors was diluted toward zero, so the two
        # headline numbers on the readiness screen moved in opposite wrong directions.
        #
        # `passed = None` means "not a result", which `profile._exams` already understands.
        row.passed = row.wrong <= row.max_errors and row.answered == row.question_count
    await session.flush()
    if row.mode == MODE_EXAM:
        await events.record(
            session, EV_EXAM_FINISHED, chat_id=row.chat_id,
            session_id=row.id, state=state, answered=row.answered,
            wrong=row.wrong, passed=row.passed,
        )


async def finish(
    session: AsyncSession, user, row: QuizSession, now: datetime | None = None
) -> QuizSession:
    """End a sitting on purpose — Submit in exam mode, End test in practice.

    Idempotent: a retried finish returns the already-graded session rather than erroring,
    because the client cannot tell a lost response from a rejected one.
    """
    now = now or datetime.now(timezone.utc)
    if row.state != SESSION_OPEN:
        return row
    await _grade(session, row, state=SESSION_SUBMITTED, finished_at=now)
    return row


async def results(
    session: AsyncSession, row: QuizSession, user=None, entitlement=None
) -> dict:
    """The reveal. Refused while the session is open — this is the endpoint the whole
    no-feedback design exists to protect.

    Carries the QUESTIONS, not just which ordinals were wrong. Without them the client
    could tell a candidate they got eleven wrong and nothing else — no way to see which,
    no way to learn anything from a failed sitting. That is the moment a learner most
    wants the material, and it was the one moment the app withheld it.

    The statement and the correct answer are FREE: they are ministerial content and the
    free tier already includes every question. Only the explanation is Premium, and it is
    fetched separately by the client through the existing gated path.
    """
    if row.state == SESSION_OPEN:
        raise SessionError("this session is still open", status=409)

    items = list(await session.scalars(
        select(QuizSessionItem)
        .where(QuizSessionItem.session_id == row.id)
        .order_by(QuizSessionItem.ordinal)
    ))
    questions = {
        q.id: q
        for q in await session.scalars(
            select(Question).where(Question.id.in_([i.question_id for i in items]))
        )
    }
    # Translations only for someone entitled to them, and only when they asked for them:
    # the review screen must not become a way to read a paid feature for free.
    lang = getattr(user, "lang", None)
    show_translation = bool(
        entitlement is not None
        and entitlement.can_translate
        and getattr(user, "translations_on", False)
        and lang in TRANSLATION_LANGUAGES
    )
    translations = {}
    if show_translation:
        rows = await session.scalars(
            select(Translation).where(
                Translation.question_id.in_(list(questions)), Translation.lang == lang
            )
        )
        translations = {t.question_id: t for t in rows}
    return {
        "session_id": row.id,
        "mode": row.mode,
        "state": row.state,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "question_count": row.question_count,
        "answered": row.answered,
        "wrong": row.wrong,
        "max_errors": row.max_errors,
        "passed": row.passed,
        "items": [
            {
                "statement": (questions[i.question_id].statement_it
                              if i.question_id in questions else ""),
                "stem": (questions[i.question_id].stem_it
                         if i.question_id in questions else None),
                "answer": (questions[i.question_id].answer
                           if i.question_id in questions else None),
                "image": (questions[i.question_id].image_path
                          if i.question_id in questions else None),
                "translation": (translations[i.question_id].statement
                                if i.question_id in translations else None),
                "ordinal": i.ordinal,
                "question_id": i.question_id,
                "given": i.given,
                "correct": i.correct,
            }
            for i in items
        ],
    }
