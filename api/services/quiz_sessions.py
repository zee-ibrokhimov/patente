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

from api.models import QuizSession, QuizSessionItem
from api.services import events, selection
from shared.constants import (
    EV_EXAM_FINISHED,
    EV_EXAM_STARTED,
    EXAM_MAX_ERRORS,
    EXAM_MINUTES,
    EXAM_QUESTIONS,
    MODE_EXAM,
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
    count = EXAM_QUESTIONS if is_exam else EXAM_QUESTIONS
    paper = await selection.exam_paper(session, count)
    if not paper:
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


async def _grade(
    session: AsyncSession,
    row: QuizSession,
    state: str,
    finished_at: datetime,
) -> None:
    row.state = state
    row.finished_at = finished_at
    if row.max_errors is not None:
        # Unanswered questions count against you, exactly as in the real exam: running
        # out of time is a way to fail, not a way to defer.
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


async def results(session: AsyncSession, row: QuizSession) -> dict:
    """The reveal. Refused while the session is open — this is the endpoint the whole
    no-feedback design exists to protect."""
    if row.state == SESSION_OPEN:
        raise SessionError("this session is still open", status=409)

    items = await session.scalars(
        select(QuizSessionItem)
        .where(QuizSessionItem.session_id == row.id)
        .order_by(QuizSessionItem.ordinal)
    )
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
                "ordinal": i.ordinal,
                "question_id": i.question_id,
                "given": i.given,
                "correct": i.correct,
            }
            for i in items
        ],
    }
