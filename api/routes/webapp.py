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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from functools import partial

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.deps import get_session
from api.models import User
from api.routes import quiz as quiz_route
from api.routes import users as users_route
from api.schemas import (
    CategoryOut,
    CoachOut,
    OwnTermIn,
    OwnTermPatch,
    SuggestionIn,
    AnalysisOut,
    ExamAnswerOut,
    ExplanationOut,
    PracticeAnswerOut,
    QuestionOut,
    QuestionTranslationOut,
    SessionAnswerIn,
    SessionOut,
    LeaderboardOut,
    SessionResultsOut,
    StartSessionIn,
    ProfileOut,
    StatsOut,
    TopicOut,
    UserOut,
    UserSettingsIn,
    VocabAnswerIn,
    VocabAnswerOut,
    VocabListOut,
    VocabRoundOut,
    ReportIn,
    ResetPreviewOut,
    VocabStatsOut,
)
from api.services import (
    categories,
    coaching,
    display_name,
    suggestions,
    analysis,
    pacing,
    channel,
    content,
    explanations,
    leaderboard,
    profile as profile_service,
    quiz_sessions,
    telegram_auth,
    translations,
    users,
    reset as reset_service,
    vocab as vocab_service,
)
from api.services.entitlement import evaluate
from api.services.telegram_auth import InitDataRejected
from shared.constants import (
    MODE_EXAM,
    MODE_OFFERS_EXPLANATION,
    QUIZ_MODES,
    REPEAT_SOURCES,
    UI_LANGUAGES,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webapp", tags=["webapp"])

# How many of a paper's TRANSLATIONS to warm at creation. Small on purpose — see the
# comment in start_session.
#
# Translations only. Explanations are deliberately never warmed; the note in start_session
# explains the arithmetic that decided it.
WARM_AHEAD = 5

# Telegram hands the client this blob when the Mini App opens; the client sends it back
# on every request. It must travel verbatim — re-encoding it breaks the HMAC.
INIT_DATA_HEADER = "X-Telegram-Init-Data"


async def webapp_user(
    background: BackgroundTasks,
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

    # Keep the name Telegram signed for, for the leaderboard and nothing else.
    #
    # Refreshed on every visit rather than written once at creation: a name is something
    # people change, and a stale one is being shown to STRANGERS. Trimmed and bounded
    # because it is rendered in other learners' apps — a 200-character "name" is a way to
    # break someone else's screen, and Telegram does not promise a length.
    #
    # Written even for someone who has opted out. The opt-out governs whether they are
    # RANKED (see leaderboard.py); throwing the name away as well would mean it silently
    # went missing if they ever opted back in.
    # `.strip()[:32]` handled length and nothing else. A first name is user-controlled text
    # over the whole of Unicode, and this is the only string in the product that one learner
    # types and another reads — see api/services/display_name.py for what is removed and why
    # emoji deliberately are not.
    name = display_name.clean(telegram.first_name)

    # WHAT GOES IN HERE NEVER TOUCHES THE REQUEST'S OWN TRANSACTION. See
    # `_store_warm_fields` — assigning these to `user` is what took the app down on
    # 2026-08-10, and it is a two-line change to put it back.
    warm: dict = {}
    if name != user.display_name:
        warm["display_name"] = name

    # Channel membership is a source of Premium, so it has to be known before entitlement
    # is evaluated — but only the FIRST check blocks. Someone whose status has never been
    # looked up would otherwise be told they are not Premium while sitting in the channel
    # they paid to join, which is the worst possible first impression. After that it
    # refreshes in the background on a 15-minute TTL and the request never waits.
    if user.channel_status is None:
        status = await channel.fetch_status(user.chat_id)
        # A failed lookup leaves the previous answer alone and does NOT stamp the timer, so
        # the next request tries again rather than trusting a value we could not confirm.
        if status is not None:
            warm["channel_status"] = status
            warm["channel_checked_at"] = datetime.now(timezone.utc)
    elif channel.is_stale(user.channel_checked_at):
        background.add_task(_refresh_channel, user.chat_id)

    if warm and await _store_warm_fields(user.chat_id, warm):
        # Read back what was just committed elsewhere. Two things depend on this: the
        # entitlement in THIS response reflects a membership discovered a moment ago, and
        # the object is left clean, so nothing is waiting to be flushed into a later query.
        await session.refresh(user)
    return user


# A user-facing request may wait at most this long for the write lock before giving up on
# keeping a cache warm. Deliberately a fraction of the 5 s global busy_timeout: the point of
# the fix is that the learner never waits on optional work, and a retry that blocks for five
# seconds is a spinner, which is the same failure wearing a different hat.
WARM_LOCK_WAIT_MS = 250

_warm_factory: async_sessionmaker[AsyncSession] | None = None


def _warm_sessions() -> async_sessionmaker[AsyncSession]:
    """A tiny pool of its own, for warm writes only.

    THE FIRST VERSION OF THIS FIX TOOK CONNECTIONS FROM THE MAIN POOL AND DEADLOCKED.

    The request already holds one connection for its whole lifetime, so opening a second
    inside it means every concurrent request needs two. The default pool is 5 + 10 overflow,
    so at ten parallel requests all fifteen connections were held by requests that were each
    waiting for a sixteenth — `QueuePool limit of size 5 overflow 10 reached, connection
    timed out, timeout 30.00`. Not slow: stuck, for thirty seconds, which is worse than the
    outage this was written to fix.

    One connection, no overflow, and a quarter-second to get it. Warm writes therefore queue
    among THEMSELVES and can never take a connection a real request needs. Failing to get
    one is treated exactly like failing to get the write lock — skip it, the next request
    recomputes it.
    """
    global _warm_factory
    if _warm_factory is None:
        from shared.config import settings
        from shared.db import make_async_engine

        engine = make_async_engine(
            settings.database_url, pool_size=1, max_overflow=0,
            pool_timeout=WARM_LOCK_WAIT_MS / 1000,
        )
        _warm_factory = async_sessionmaker(engine, expire_on_commit=False)
    return _warm_factory


async def _store_warm_fields(chat_id: int, values: dict) -> bool:
    """Persist derived, cached fields — and never fail the request that noticed them.

    THIS FUNCTION EXISTS BECAUSE OF A PRODUCTION OUTAGE (2026-08-10 23:19-23:21). Fifty
    `database is locked` 500s in two minutes, on every screen the owner opened.

    `webapp_user` runs on every authenticated request and keeps two cached fields fresh: the
    Telegram display name and the channel-membership status. It used to assign them straight
    onto `user`, on the REQUEST's session. Every route begins with a SELECT, so by then the
    transaction is already a reader; the assignment then had to be flushed as an UPDATE, and
    that is a transaction upgrade.

        SQLite refuses a deferred read->write upgrade IMMEDIATELY, and does not honour
        busy_timeout on that path.

    It cannot wait — two readers both waiting to upgrade is a deadlock — so it fails one
    instead. `PRAGMA busy_timeout=5000` was set, live and verified in production, and was
    never consulted. The Mini App fires several requests at once and the admin console fires
    six, so one learner opening one screen produced six writers to one row.

    Two things fix it, and both are needed:

      · its OWN transaction, whose first statement is the UPDATE. A transaction that opens
        by writing takes the lock there and then, which is the case where busy_timeout does
        apply — so this waits its turn instead of failing instantly;
      · the request's session is left with nothing pending, so no later SELECT in any route
        can autoflush a write into itself. That is why six DIFFERENT endpoints all broke
        from one line in a shared dependency, and why tolerating the error alone would only
        have moved the failure somewhere harder to find.

    Losing one of these writes costs nothing: every one of them is recomputed from scratch
    on the next request, and there is a test that the name really does land the second time.
    Never trying at all would be a different bug, and there are tests for that too.
    """
    from sqlalchemy import text, update

    try:
        async with _warm_sessions()() as own:
            # Scoped to this connection and restored below, because connections are pooled
            # and a 250 ms timeout left behind would silently apply to real writes.
            await own.execute(text(f"PRAGMA busy_timeout = {WARM_LOCK_WAIT_MS}"))
            try:
                await own.execute(
                    update(User).where(User.chat_id == chat_id).values(**values))
                await own.commit()
            finally:
                await own.execute(text("PRAGMA busy_timeout = 5000"))
        return True
    except (OperationalError, PoolTimeout) as exc:
        # Both are "somebody else is busy", and both are expected: OperationalError is the
        # write lock, PoolTimeout is the single warm connection already in use. Neither is
        # something to page anybody about, hence INFO — but logged, because a cache that
        # stopped warming permanently would otherwise be completely invisible.
        log.info("skipped a warm write for %s (%s): %s", chat_id, ",".join(values), exc)
        return False


async def _refresh_channel(chat_id: int) -> None:
    """Background refresh of the membership cache, after the response has gone out.

    Its own session for the reason quiz.py documents: FastAPI runs a yield-dependency's exit
    code AFTER background tasks, so borrowing the request's session would hold a transaction
    open across a network call to Telegram.

    The lookup happens here; the WRITE goes through `_store_warm_fields`, so a background
    task cannot take the lock in a way that fails whatever request is in flight beside it.
    """
    from shared.db import async_session_factory

    factory = async_session_factory()
    async with factory() as own:
        user = await own.get(User, chat_id)
        if user is None:
            return
        status = await channel.fetch_status(chat_id)
    if status is None:
        return
    await _store_warm_fields(
        chat_id, {"channel_status": status,
                  "channel_checked_at": datetime.now(timezone.utc)})


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


# POST /webapp/answers IS DELETED, AND ITS ABSENCE IS THE POINT.
#
# It accepted ANY question id, with no sitting and no ownership check, and returned
# `correct_answer` in the response. So the public internet had a free, exact oracle for the
# whole 7,106-question bank: two requests per question, about 2,000 requests to own the lot.
# Every threshold in the product — the streak's ten a day, the hundred answers that unlock
# the analysis, twenty points to hold a rank, and now a league point per question — was
# therefore a count of HTTP requests rather than of studying.
#
# It had no caller. `api.answer` exists in webapp/src/api.ts and nothing in main.ts invokes
# it; the only answer path in the client is `sessions.answer`, inside a real sitting. The bot
# does not use it either. It was reachable, useful only to somebody attacking the product,
# and dead.
#
# The loopback twin POST /users/{chat_id}/answers stays: it takes its identity from the URL,
# which is exactly why nginx proxies only /webapp/* and the Tribute webhook, and why that
# surface must never be published. Tests exercise the write path through it.


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


@router.post("/analysis/coach", response_model=CoachOut)
async def coach(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Study advice on top of the breakdown.

    POST rather than GET because it may spend money — a GET that bills is a GET a browser,
    a prefetcher or a retry will fire on its own.

    Every refusal is a 200 with a state, never an error: the breakdown underneath is the
    screen, and an AI layer that can break the page it sits on is not worth the page.
    """
    now = datetime.now(timezone.utc)
    entitlement = evaluate(user)

    allowed, why = await coaching.may_generate(session, user, entitlement, now)
    if not allowed:
        last = await coaching.latest(session, user.chat_id)
        body = dict(last.body) if last is not None else {}
        available = None
        if last is not None and why == "cooldown":
            made = last.created_at
            if made.tzinfo is None:
                made = made.replace(tzinfo=timezone.utc)
            available = made + coaching.COOLDOWN
        # The previous analysis comes back with the refusal. A learner inside the cooldown
        # should re-read what they were given, not stare at a locked button.
        return CoachOut(state=why, available_at=available, **body)

    report = await analysis.report(session, user.chat_id, now)
    produced = await coaching.generate(session, user, report, now)
    if produced is None:
        return CoachOut(state="unavailable")
    return CoachOut(state="ready", **produced)


@router.post("/vocab/terms", status_code=201)
async def add_own_term(
    body: OwnTermIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """A word the learner adds for themselves. Nobody else ever sees it."""
    try:
        row = await vocab_service.add_own(
            session, user, body.it, body.gloss, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"id": row.id}


@router.patch("/vocab/terms/{term_id}")
async def edit_own_term(
    term_id: int,
    body: OwnTermPatch,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await vocab_service.edit_own(
            session, user, term_id, body.it, body.gloss, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return {"id": row.id, "it": row.it, "gloss": row.gloss(user.lang)}


@router.delete("/vocab/terms/{term_id}", status_code=204)
async def delete_own_term(
    term_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    try:
        await vocab_service.delete_own(session, user, term_id, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    return None


@router.post("/suggestions", status_code=201)
async def suggest(
    body: SuggestionIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """"What should we add?" from the top of Settings.

    Stores the text, the language it was written in and who wrote it, and nothing else. The
    owner needs to reply and to know which of four languages to reply in; anything more
    would be collected because it was easy rather than because it was needed.
    """
    try:
        row = await suggestions.submit(session, user.chat_id, body.text, user.lang)
    except suggestions.Refused as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": row.id}


@router.get("/analysis", response_model=AnalysisOut)
async def analysis_report(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """Where the learner is losing marks.

    Free, deliberately. It is the screen that makes the paid AI analysis worth tapping, and
    a paywall in front of the diagnosis would leave a learner with a percentage and no idea
    what to do about it — which is the state this endpoint exists to end.
    """
    return AnalysisOut(**await analysis.report(session, user.chat_id))


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


def _session_out(row, paper, now: datetime,
                 answered_ordinals: list[int] | None = None) -> SessionOut:
    return SessionOut(
        id=row.id, mode=row.mode, state=row.state,
        started_at=row.started_at, expires_at=row.expires_at, server_now=now,
        question_count=row.question_count, max_errors=row.max_errors,
        answered=row.answered, questions=paper,
        answered_ordinals=answered_ordinals or [],
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
    if body.source not in REPEAT_SOURCES:
        raise HTTPException(422, f"source must be one of {REPEAT_SOURCES}")
    # Refused, not ignored. A scope the server does not recognise would otherwise widen the
    # sitting silently back to the whole bank, which looks to the learner like the subject
    # they picked being wrong rather than like an error.
    if body.scope is not None and not categories.is_valid(body.scope):
        raise HTTPException(422, "unknown scope")
    if body.mode == MODE_EXAM and body.scope:
        raise HTTPException(422, "an exam is always drawn from the whole bank")

    now = datetime.now(timezone.utc)
    try:
        row, paper = await quiz_sessions.create(
            session, user, body.mode, now, source=body.source, scope=body.scope)
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

    # EXPLANATIONS ARE DELIBERATELY *NOT* WARMED. Do not add it back.
    #
    # An audit found that `explanations.warm` had become unreachable — its only call site
    # was an endpoint nothing calls — and warming was briefly added here to fix that. The
    # owner reversed it the same day, on cost, and they are right.
    #
    # The arithmetic: a translation is ~150 tokens in and ~300 out, and EVERY non-Italian
    # learner needs one on EVERY question, so warming it ahead is cheap and always used.
    # An explanation is ~8000 tokens in, because the statute goes in the prompt, and only
    # some fraction of learners ever tap "Why?". Warming the next five questions therefore
    # buys a faster explanation for the few who ask by paying for it for everyone who does
    # not — on a product with a handful of users and no revenue yet, that is the wrong
    # trade, and it is unbounded per Start tap.
    #
    # So an explanation is generated at the moment someone asks for it, and cached per
    # CLUSTER for everyone afterwards. The cost is a one-time ~5s wait for the first person
    # to ask about that rule; the saving is never paying for the 3370 clusters nobody
    # reaches. Revisit only when traffic makes the cache fill itself.
    return _session_out(row, payloads, now)


class VocabRecallIn(BaseModel):
    """A self-graded card. No text: the learner did not type anything, and pretending
    otherwise would put an answer they never gave into the grading path."""

    term_id: int
    knew: bool


class PrefetchIn(BaseModel):
    """Which slice of the paper to prepare."""

    from_ordinal: int = Field(ge=1)
    count: int = Field(default=5, ge=1, le=10)
    # Whether to answer only once the work is DONE.
    #
    # The loading screen at the start of a quiz needs this; the rolling top-ups must not
    # have it. Someone watching "preparing your questions" wants that screen to end when the
    # questions are genuinely ready. Someone reading question 3 while the next five are
    # fetched wants it to cost them nothing. Same endpoint, opposite requirement — so the
    # caller says which, rather than the endpoint guessing.
    wait: bool = Field(default=False)


# How long the loading screen is allowed to hold the request open.
#
# Not a cancellation: whatever is still running when this expires KEEPS running and lands in
# the cache for whoever reads that question next. The deadline bounds how long a learner
# stares at a spinner, not how much work gets done — cancelling would throw away a
# half-finished paid model call, which is the one outcome with no upside.
PREFETCH_DEADLINE = 75.0

# How many warms may be in flight at once inside one waited prefetch.
#
# A five-question window queues up to ten model calls — five translations and five
# explanations. Fired together they arrive as one burst against an account limited to
# 30,000 tokens per minute, and an explanation prompt alone runs 7,000-14,000: the burst
# does not go faster, it goes 429. Three at a time keeps the request saturated without
# tripping the ceiling. Raise it when the account's TPM is raised, not before.
PREFETCH_CONCURRENCY = 3

# Tasks outstanding past the deadline. asyncio keeps only weak references to running tasks,
# so without a strong one here the garbage collector is free to destroy a warm() mid-flight
# — the failure mode being that prefetch works under load and silently drops work when idle.
_detached: set[asyncio.Task] = set()


def _detach(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _detached.add(task)
    task.add_done_callback(_detached.discard)
    return task


@router.post("/sessions/{session_id}/prefetch")
async def prefetch(
    session_id: int,
    body: PrefetchIn,
    background: BackgroundTasks,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Prepare the next few questions: translations, and explanations for their clusters.

    The learner is going to read every question in this paper — it is frozen at creation —
    so fetching a few ahead is not speculative. It is the same work, done before they are
    waiting for it rather than while.

    That is what makes this different from the warming removed on 2026-08-09. That version
    generated explanations for a window regardless of anything; this one is bounded by a
    paper the learner has already committed to, and it exists because a loading screen at
    the start of a quiz is a place where waiting is EXPECTED and therefore free.

    Runs detached and answers immediately. The client shows its loading state for as long as
    it chooses and never blocks on this returning — a prefetch that fails is a slower
    question later, not a broken quiz.
    """
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    entitlement = evaluate(user)
    items = await quiz_sessions.window(
        session, row, body.from_ordinal, body.count)

    # Committed before scheduling, for the reason start_session documents: FastAPI runs a
    # yield-dependency's exit code AFTER background tasks, so an open write transaction
    # would be held while the warmers open their own connections to the same SQLite file.
    await session.commit()

    jobs: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    for question_id, cluster_id in items:
        if entitlement.can_translate and user.translations_on:
            jobs.append(("translation", partial(translations.warm, question_id)))
        # Explanations only where they can be shown, and only in a mode that shows them —
        # an exam never offers one, so preparing thirty would be paid calls for text nobody
        # is ever given.
        if (cluster_id is not None
                and entitlement.can_explain
                and MODE_OFFERS_EXPLANATION.get(row.mode)):
            jobs.append(("explanation",
                         partial(explanations.warm, cluster_id,
                                 translations.reading_language(user))))

    warmed_translations = sum(1 for kind, _ in jobs if kind == "translation")
    warmed_explanations = sum(1 for kind, _ in jobs if kind == "explanation")

    ready = pending = 0
    if body.wait:
        # Actually do the work before answering. This used to hand every job to
        # BackgroundTasks and return at once — which meant the response was ready in
        # milliseconds and reported counts for work that had not started, so a client could
        # not wait for the prefetch even in principle. The loading screen was therefore
        # timing out against a promise, not against the model.
        #
        # asyncio.wait rather than wait_for: on the deadline the unfinished jobs are left
        # RUNNING, not cancelled. Each warm() owns its session and swallows its own errors,
        # so a straggler finishing after the response simply populates the cache early for
        # whoever reads that question next.
        # WAIT FOR TRANSLATIONS ONLY. Explanations start now and are not waited on.
        #
        # They are needed at different moments. The translation is how the question is READ,
        # so it has to be there before the question appears. The explanation is only wanted
        # after answering, if the learner taps "Why?" — by which point it has had the whole
        # time they spent reading and deciding.
        #
        # Waiting on both is what made the loading screen unusable. Measured cold on a
        # five-question window: ten jobs, three at a time, hit the 75-second deadline with
        # FOUR unfinished — explanations at 28s each were most of it. The user's report was
        # "in question 4 there was no translation and i waited again", which is precisely
        # what the last two unfinished jobs look like from the outside.
        #
        # Explanations still get their head start; they are simply not something to make
        # somebody watch a spinner for.
        gate = asyncio.Semaphore(PREFETCH_CONCURRENCY)

        async def bounded(run):
            async with gate:
                await run()

        blocking = [_detach(bounded(run)) for kind, run in jobs if kind == "translation"]
        for kind, run in jobs:
            if kind != "translation":
                _detach(bounded(run))

        if blocking:
            finished, unfinished = await asyncio.wait(blocking, timeout=PREFETCH_DEADLINE)
            ready, pending = len(finished), len(unfinished)
    else:
        for _kind, run in jobs:
            background.add_task(run)

    return {
        "from_ordinal": body.from_ordinal,
        "questions": len(items),
        "translations": warmed_translations,
        "explanations": warmed_explanations,
        # What the caller actually got. `translations`/`explanations` are how much work was
        # QUEUED — they have always been that, and read as a completion count.
        "waited": body.wait,
        "ready": ready,
        "pending": pending,
    }


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
    items = (await quiz_sessions.results(session, row, user, entitlement)
             if row.state != "open" else None)
    paper = []
    done: list[int] = []
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

        # WHICH ORDINALS ARE ALREADY ANSWERED — and only which.
        #
        # The resume payload carried `answered` as a COUNT and nothing else, and the client
        # zeroes its own record on every reopen (`answered: new Set()`), with no local
        # storage to restore it from. So a learner who answered 20 of 30, took a phone call
        # and came back got the right question and the right clock above an answer sheet
        # with all thirty circles blank — the sheet being the only progress indicator on
        # the screen, and its stated purpose being "showing which you have done".
        #
        # Nothing was ever lost: the server refuses a second answer for the same ordinal
        # with a 409 before any counter moves, and Submit still returns all thirty. But for
        # the rest of a timed exam the learner has no way to know that, and the obvious
        # reaction — start again — abandons the sitting.
        #
        # ORDINALS ONLY. Not `given`, not `correct`. Returning those would put a hole in
        # "an exam reveals nothing until it is over", which ExamAnswerOut exists to defend.
        # Knowing you answered question 7 is not knowing whether you got it right.
        done = list(await session.scalars(
            select(QuizSessionItem.ordinal)
            .where(QuizSessionItem.session_id == row.id,
                   QuizSessionItem.given.is_not(None))
            .order_by(QuizSessionItem.ordinal)
        ))
    return _session_out(row, paper, now, done)


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
    entitlement = evaluate(user)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
        payload = await quiz_sessions.answer(
            session, user, row, body.ordinal, body.answer, entitlement, now
        )
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except pacing.TooFast as exc:
        raise HTTPException(429, str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from exc

    if row.mode == MODE_EXAM:
        return ExamAnswerOut(**payload)

    # No explanation is generated here either — see the note in start_session. Answering
    # reports whether one CAN be offered; producing it waits until the learner taps "Why?",
    # so the bill follows the people who actually read them.
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
        return SessionResultsOut(
            **await quiz_sessions.results(session, row, user, evaluate(user))
        )
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/sessions/{session_id}/exit", response_model=SessionResultsOut)
async def exit_session(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Leave an exam without sitting it, and get the review anyway.

    Returns the same payload as `finish` on purpose: the learner still wants to know how the
    questions they DID answer went, and that is the one moment they most want the material.
    What differs is `passed`, which stays null — the client renders that as "not counted"
    rather than as a fail, and the profile never counts the sitting at all.

    Idempotent, like finish: a retried exit returns the review rather than an error.
    """
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
        await quiz_sessions.abandon(session, user, row, now)
        return SessionResultsOut(
            **await quiz_sessions.results(session, row, user, evaluate(user))
        )
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/sessions/{session_id}/extend", response_model=SessionOut)
async def extend_session(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Add another batch to a practice sitting the learner has worked to the end of.

    Practice runs until they stop it; only the transport is batched. Refused for an
    exam, in the service rather than here — an exam whose length the client can extend
    reports a score that means nothing.
    """
    now = datetime.now(timezone.utc)
    try:
        row = await quiz_sessions.load_owned(session, user, session_id, now)
        await quiz_sessions.extend(session, user, row, now)
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    entitlement = evaluate(user)
    from sqlalchemy import select as sa_select

    from api.models import Question, QuizSessionItem

    rows = await session.scalars(
        sa_select(Question)
        .join(QuizSessionItem, QuizSessionItem.question_id == Question.id)
        .where(QuizSessionItem.session_id == row.id)
        .order_by(QuizSessionItem.ordinal)
    )
    paper = [await content.question_payload(session, q, user, entitlement) for q in rows]
    return _session_out(row, paper, now)


@router.get("/sessions/{session_id}/results", response_model=SessionResultsOut)
async def session_results(
    session_id: int,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """The reveal, refused while the sitting is still open."""
    try:
        row = await quiz_sessions.load_owned(session, user, session_id)
        return SessionResultsOut(
            **await quiz_sessions.results(session, row, user, evaluate(user))
        )
    except quiz_sessions.SessionError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


# --- vocabulary trainer -----------------------------------------------------
#
# Premium, and gated inside api/services/vocab.py rather than here — a route is a
# surface, not a policy. These handlers do two things only: translate a VocabError into
# the status it carries, and hand the service the caller that initData proved.


@router.get("/vocab/round", response_model=VocabRoundOut)
async def vocab_round(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """A round of terms to type, mixed in both directions.

    The response carries prompts and no answers. That is deliberate and load-bearing:
    a paper that shipped its own answer key would be readable in the network tab, and
    a test you can read the answers off is not a test.
    """
    try:
        return await vocab_service.round_for(session, user, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/vocab/answer", response_model=VocabAnswerOut)
async def vocab_answer(
    body: VocabAnswerIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """Grade one typed answer. Near-misses come back as `almost` with the correct form."""
    try:
        return await vocab_service.answer(
            session, user, body.term_id, body.direction, body.given, evaluate(user)
        )
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/vocab/terms", response_model=VocabListOut)
async def vocab_terms(
    q: str = Query(default="", max_length=100),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """The searchable word list — the reference half of the feature."""
    try:
        return await vocab_service.browse(session, user, evaluate(user), q, offset, limit)
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.get("/vocab/stats", response_model=VocabStatsOut)
async def vocab_stats(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """Progress through the list. Outside the paywall on purpose: this is the number
    that makes the feature worth buying, so hiding it behind itself is a poor trade."""
    return await vocab_service.stats(session, user)


# --- starting over ----------------------------------------------------------


@router.get("/categories", response_model=list[CategoryOut])
async def categories_list(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """The seven subjects a learner can practise, worst first.

    Ranked by the marks each is costing THEM, not by size, and computed by
    `analysis.families` — the same call the error-analysis screen uses. Two screens quoting
    the same quantity from two different calculations is two screens that eventually
    disagree, and the learner cannot tell which to believe.
    """
    return await categories.catalogue(session, user.chat_id)


@router.get("/leaderboard", response_model=LeaderboardOut)
async def leaderboard_board(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """This week's league table, Monday to Sunday UTC, ranked on correct answers.

    The ONLY response in this product that carries one learner's data to another. Everything
    that decides what may be shown lives in the service rather than here, so a second caller
    cannot get it wrong — see api/services/leaderboard.py.
    """
    return LeaderboardOut(**await leaderboard.board(session, user))


@router.get("/reset/preview", response_model=ResetPreviewOut)
async def reset_preview(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """What a reset would destroy, so the confirmation can name real numbers."""
    return await reset_service.preview(session, user.chat_id)


@router.post("/reset", response_model=ResetPreviewOut)
async def reset_progress(
    user: User = Depends(webapp_user), session: AsyncSession = Depends(get_session)
):
    """Wipe this learner's progress and start again.

    POST rather than DELETE because it destroys several things and creates an event; and
    the caller is whoever the initData signature proves, so one user can only ever reset
    themselves — there is no id in the request to get wrong.

    A pass is NOT touched. Wiping progress must never cost someone money they have paid.
    """
    return await reset_service.reset_progress(session, user.chat_id)


@router.post("/reports", status_code=201)
async def submit_report(
    body: ReportIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """"This explanation is wrong."

    The whole path was built, tested and reachable only from the loopback route the bot
    used — so since drilling moved into the Mini App, a paying user who read a wrong
    AI-written explanation had no way to say so, and the owner had no way to hear it.

    Report volume per thousand explanations served is the quality metric for a feature
    whose entire pitch is quality (plan §9), and it was structurally impossible to collect.
    """
    return await quiz_route.submit_report(body=body, user=user, session=session)


@router.get("/vocab/cards", response_model=None)
async def vocab_cards(
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """A round of flip cards — same terms as the typing round, with the answer attached.

    Separate from /vocab/round because that one withholds the answer on purpose. Here the
    answer IS the interaction, and fetching it per flip would put a network round trip
    between a tap and the thing the learner tapped for.
    """
    try:
        return await vocab_service.cards(session, user, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc


@router.post("/vocab/recall", response_model=None)
async def vocab_recall(
    body: VocabRecallIn,
    user: User = Depends(webapp_user),
    session: AsyncSession = Depends(get_session),
):
    """"I knew it" / "I didn't", from a flip card."""
    try:
        return await vocab_service.recall(
            session, user, body.term_id, body.knew, evaluate(user))
    except vocab_service.VocabError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
