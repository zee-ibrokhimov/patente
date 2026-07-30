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

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import User
from api.routes import quiz as quiz_route
from api.routes import users as users_route
from api.schemas import (
    AnswerIn,
    AnswerOut,
    ExplanationOut,
    QuestionOut,
    QuestionTranslationOut,
    StatsOut,
    TopicOut,
    UserOut,
    UserSettingsIn,
)
from api.services import telegram_auth, users
from api.services.telegram_auth import InitDataRejected
from shared.constants import UI_LANGUAGES

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webapp", tags=["webapp"])

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
async def me(user: User = Depends(webapp_user)):
    """Who the caller is, plus their entitlement. The frontend renders from this and
    decides nothing itself — every paid response is gated server-side regardless."""
    return users_route._out(user)


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
