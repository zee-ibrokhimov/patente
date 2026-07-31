"""The Mini App's API surface — the only part of this service safe to make public.

Everything under /users/{chat_id}/... takes its identity from the URL, which is why
that surface must never leave loopback: the caller simply asserts who they are. These
routes are the same operations with that one property inverted — **the chat id comes
from a Telegram-signed payload and is never read from the request**. There is no
`chat_id` parameter anywhere in this file, and that is the invariant to preserve.

Deliberately NOT re-exposed here, because a public route to any of them would be a
way to give away the product or destroy data:

  · POST   /users/{chat_id}/pass   — grants an unlimited paid pass
  · DELETE /users/{chat_id}        — erases a user and their progress
  · PUT    /figures/{name}/file-id — writes to the Telegram file_id cache
  · GET    /health, /docs          — nothing an end user needs

Each handler delegates to the existing route function rather than repeating its body.
That keeps the promise in plan §6.1 — no business logic in a client surface, and one
implementation of every rule. If entitlement or Leitner behaviour changes, it changes
in one place and both surfaces follow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import User
from api.routes import quiz as quiz_route
from api.routes import users as users_route
from api.schemas import (
    AnswerIn,
    AnswerOut,
    ExamAnswerOut,
    ExplanationOut,
    PracticeAnswerOut,
    QuestionOut,
    QuestionTranslationOut,
    SessionAnswerIn,
    SessionOut,
    SessionResultsOut,
    StartSessionIn,
    ProfileOut,
    StatsOut,
    TopicOut,
    UserOut,
    UserSettingsIn,
)
from api.services import (
    content,
    profile as profile_service,
    quiz_sessions,
    telegram_auth,
    translations,
    users,
)
from api.services.entitlement import evaluate
from api.services.telegram_auth import InitDataRejected
from shared.constants import MODE_EXAM, QUIZ_MODES, UI_LANGUAGES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webapp", tags=["webapp"])

# How many of a paper's translations to warm at creation. Small on purpose - see the
# comment in start_session.
WARM_AHEAD = 5

# Telegram hands the client this blob when the Mini App opens; the client sends it back
# on every request. It must travel verbatim — re-encoding it breaks the HMAC.
INIT_DATA_HEADER = "X-Telegram-Init-Data"


async def webapp_user(
    init_data: str | None = Header(default=None, alias=INIT_DATA_HEADER),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the caller from their signed initData, or refuse.

    The user is created if this is their first appearance. The Mini App is opened from
    the bot, so /start has normally happened already — but losing a session to a 404
    because onboarding raced is worse than an extra row, and get_or_create is idempotent.

    The 401 body is deliberately vague. Which check failed (bad signature, stale,
    malformed) is useful to an attacker probing the endpoint and useless to a real
    client, so the detail goes to the log instead.
    """
    try:
        telegram = telegram_auth.verify(init_data or "")
    except InitDataRejected as exc:
        log.warning("rejected a Mini App request: %s", exc)
        raise HTTPException(401, "invalid initData") from exc

    # Telegram reports the client's language, which may be anything (uk, de, …).
    # Only offer it as a default if we actually speak it.
    lang = telegram.language_code if telegram.language_code in UI_LANGUAGES else "it"
    user, _created = await users.get_or_create(session, telegram.chat_id, lang)
    return user


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)):
    """Who the caller is, plus their entitlement. The frontend renders from this and
    decides nothing itself — every paid response is gated server-side regardless."""
    return await users_route._out(user, session)


@router.patch("/settings", response_model=UserOut)
async def update_settings(
    body: UserSettingsIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await users_route.update(body=body, user=user, session=session)


@router.get("/topics", response_model=list[TopicOut])
async def topics(
    _user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    return await quiz_route.list_topics(session=session)


@router.get("/next-question", response_model=QuestionOut)
async def next_question(
    background: BackgroundTasks,
    topic_id: int | None = Query(default=None),
    exclude_id: int | None = Query(default=None, description="question just answered"),
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await quiz_route.serve_next(
        background=background,
        topic_id=topic_id,
        exclude_id=exclude_id,
        user=user,
        session=session,
    )


@router.post("/answers", response_model=AnswerOut)
async def answer(
    body: AnswerIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await quiz_route.submit_answer(body=body, user=user, session=session)


@router.post("/questions/{question_id}/translation", response_model=QuestionTranslationOut)
async def translation(
    question_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await quiz_route.read_translation(
        question_id=question_id, user=user, session=session
    )


@router.post("/questions/{question_id}/explanation", response_model=ExplanationOut)
async def explanation(
    question_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    return await quiz_route.read_explanation(
        question_id=question_id, user=user, session=session
    )


@router.get("/profile", response_model=ProfileOut)
async def profile(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """Streak, readiness and exam history. Free, like stats — the screen that makes
    someone come back tomorrow should never be behind the paywall it advertises."""
    return ProfileOut(**await profile_service.user_profile(session, user.chat_id))


@router.get("/stats", response_model=StatsOut)
async def stats(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    return await quiz_route.read_stats(user=user, session=session)


# There is deliberately no figure route here. Figures are served as STATIC files by
# nginx at /figures/, because an <img src> tag sends no custom headers — anything
# behind the initData header is an unavoidable 401 for an image. They are public-domain
# ministerial figures carrying no user data, so static is both the fix and the better
# design. See webapp/Dockerfile and webapp/nginx.conf.


# --- quiz sessions ----------------------------------------------------------
#
# All four routes take the session id from the URL and the caller from the signature,
# then check that the two agree in `load_owned`. That check is the whole of the
# authorisation model here and it must not be skipped on any route added later — an id
# is a small integer and this surface is public.


def _session_out(row, paper, now: datetime) -> SessionOut:
    return SessionOut(
        id=row.id, mode=row.mode, state=row.state,
        started_at=row.started_at, expires_at=row.expires_at, server_now=now,
        question_count=row.question_count, max_errors=row.max_errors,
        answered=row.answered, questions=paper,
    )


@router.post("/sessions", response_model=SessionOut)
async def start_session(
    body: StartSessionIn,
    background: BackgroundTasks,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Start an exam or a practice run, and return the whole paper with it."""
    if body.mode not in QUIZ_MODES:
        raise HTTPException(422, f"mode must be one of {QUIZ_MODES}")

    now = datetime.now(timezone.utc)
    try:
        row, paper = await quiz_sessions.create(session, user, body.mode, now)
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    entitlement = evaluate(user)
    payloads = [
        await content.question_payload(session, q, user, entitlement) for q in paper
    ]

    # Commit before scheduling background work, for the reason quiz.py documents: FastAPI
    # runs the exit code of a yield-dependency AFTER background tasks, so leaving it
    # would hold a write transaction open while warming opens its own connection to the
    # same SQLite file — and one of the two loses to "database is locked".
    await session.commit()

    # Warm only the first few, and only for someone who can actually be shown a
    # translation. Warming the whole paper would mean thirty paid OpenAI calls the
    # instant Start is tapped — before a single answer — for a user who may abandon at
    # question three, and `translations_on` defaults to true so a FREE user would
    # trigger it too and then see every one of them as LOCKED. Nothing rate-limits
    # session creation, so that is an unbounded cost per tap.
    if entitlement.can_translate and user.translations_on:
        for q in paper[:WARM_AHEAD]:
            background.add_task(translations.warm, q.id)

    return _session_out(row, payloads, now)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def read_session(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Resume. The Mini App persists nothing across a reopen, so this is how a
    backgrounded exam comes back — with the server's deadline, not a remembered one."""
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    entitlement = evaluate(user)
    items = await quiz_sessions.results(session, row) if row.state != "open" else None
    paper = []
    if items is None:
        from api.models import Question, QuizSessionItem
        from sqlalchemy import select

        rows = await session.scalars(
            select(Question)
            .join(QuizSessionItem, QuizSessionItem.question_id == Question.id)
            .where(QuizSessionItem.session_id == row.id)
            .order_by(QuizSessionItem.ordinal)
        )
        paper = [
            await content.question_payload(session, q, user, entitlement)
            for q in rows
        ]
    return _session_out(row, paper, now)


@router.post("/sessions/{session_id}/answers", response_model=None)
async def answer_session(
    session_id: int,
    body: SessionAnswerIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
) -> ExamAnswerOut | PracticeAnswerOut:
    """Answer one item.

    The response model is built in the service, from a whitelist, rather than declared
    as a union here: making "exam reveals nothing" a pydantic filter puts the guarantee
    in the wrong place, where a new field on the practice model would silently pass
    through. The mode branch lives where the mode does.
    """
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
        payload = await quiz_sessions.answer(
            session, user, row, body.ordinal, body.answer, evaluate(user), now
        )
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    if row.mode == MODE_EXAM:
        return ExamAnswerOut(**payload)
    return PracticeAnswerOut(**payload)


@router.post("/sessions/{session_id}/finish", response_model=SessionResultsOut)
async def finish_session(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Submit an exam, or End test in practice. Idempotent."""
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
        await quiz_sessions.finish(session, user, row, now)
        return SessionResultsOut(**await quiz_sessions.results(session, row))
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/sessions/{session_id}/results", response_model=SessionResultsOut)
async def session_results(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """The reveal, refused while the sitting is still open."""
    try:
        row = await quiz_sessions.load_owned(session, user, session_id)
        return SessionResultsOut(**await quiz_sessions.results(session, row))
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
