from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, get_user
from shared.config import settings
from api.models import Purchase, User, VocabTerm
from api.schemas import GrantPassIn, UserIn, UserOut, UserSettingsIn
from api.services import users
from api.services.entitlement import evaluate
from api.services.events import record
from shared.constants import EV_PASS_GRANTED, EV_TRANSLATION_TOGGLED

router = APIRouter(prefix="/users", tags=["users"])


async def _out(user: User, session: AsyncSession) -> UserOut:
    """The user, plus whether they have ever paid.

    `purchased` is a real query rather than a relationship read: `user.purchases` is lazy,
    and touching a lazy relationship on an async session either raises MissingGreenlet or
    quietly returns nothing depending on how the object was loaded — the kind of bug that
    reads as "trials never end" in production and as nothing at all in a test.

    It matters because a trial is a pass with an expiry and no money behind it, so pass
    state alone cannot tell a paying subscriber from someone still trialling — and the
    client shows a very different screen for each.
    """
    ent = evaluate(user)

    # Newest first: starting a second sitting abandons the first, so the most recent open
    # row is the only one that can still be returned to.
    from api.models import QuizSession
    from shared.constants import SESSION_OPEN

    open_session_id = await session.scalar(
        select(QuizSession.id)
        .where(QuizSession.chat_id == user.chat_id, QuizSession.state == SESSION_OPEN)
        .order_by(QuizSession.id.desc())
        .limit(1)
    )
    # `amount_cents > 0` and not merely "a row exists": a Tribute trial writes a Purchase
    # row too, because that row's UNIQUE id is what makes webhook redelivery idempotent.
    # It is stored at zero. Counting rows instead of money would make anyone on a trial
    # read as a paying customer, and /plan would stop selling to exactly the people it
    # exists to sell to.
    purchased = bool(
        await session.scalar(
            select(func.count(Purchase.id)).where(
                Purchase.chat_id == user.chat_id, Purchase.amount_cents > 0
            )
        )
    )
    # A zero-amount Purchase row is a Tribute trial: a card IS linked and it will be
    # charged. A pass with no row at all came from /grant and can never be charged. Both
    # look identical from `has_pass` and `purchased`, and /plan has to tell them opposite
    # things — one must be warned about the charge, the other must not be told to cancel
    # a subscription that does not exist.
    trialing = bool(
        await session.scalar(
            select(func.count(Purchase.id)).where(
                Purchase.chat_id == user.chat_id, Purchase.amount_cents == 0
            )
        )
    )
    # Counted on every read rather than cached at import. It is a COUNT over ~1100 rows of
    # build-time content and costs nothing measurable, while a process-lifetime cache would
    # be wrong in exactly the place this field exists to be right: reseeding a larger
    # glossary would leave every running worker still reporting the old size.
    vocab_terms = (await session.scalar(select(func.count()).select_from(VocabTerm))) or 0
    return UserOut(
        chat_id=user.chat_id,
        lang=user.lang,
        translations_on=user.translations_on,
        pass_expires_at=user.pass_expires_at,
        has_pass=ent.has_pass,
        purchased=purchased,
        open_session_id=open_session_id,
        premium=ent.premium,
        # Most specific reason first: staff outranks a pass, and a pass is the one worth
        # naming to someone who paid for it.
        premium_via=("staff" if ent.is_staff else
                     "pass" if ent.has_pass else
                     "channel" if ent.via_channel else "none"),
        trialing=trialing,
        leaderboard_opt_out=user.leaderboard_opt_out,
        reminders_off=user.reminders_off,
        vocab_terms=vocab_terms,
        bot_username=settings.bot_username,
        support_contact=settings.support_contact,
        free_explanations_left=ent.free_explanations_left,
        onboarded_at=user.onboarded_at,
        created_at=user.created_at,
    )


@router.post("", response_model=UserOut, status_code=200)
async def create_or_get(body: UserIn, session: AsyncSession = Depends(get_session)):
    """Idempotent. /start on an existing user must not reset their progress."""
    user, _created = await users.get_or_create(session, body.chat_id, body.lang, body.source)
    return await _out(user, session)


@router.get("/{chat_id}", response_model=UserOut)
async def read(user: User = Depends(get_user), session: AsyncSession = Depends(get_session)):
    return await _out(user, session)


@router.patch("/{chat_id}", response_model=UserOut)
async def update(
    body: UserSettingsIn,
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    was_on = user.translations_on
    try:
        await users.update_settings(
            session,
            user,
            lang=body.lang,
            translations_on=body.translations_on,
            onboarded=body.onboarded,
            leaderboard_opt_out=body.leaderboard_opt_out,
            reminders_off=body.reminders_off,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    if body.translations_on is not None and body.translations_on != was_on:
        await record(
            session, EV_TRANSLATION_TOGGLED, chat_id=user.chat_id, on=body.translations_on
        )
    return await _out(user, session)


@router.post("/{chat_id}/pass", response_model=UserOut)
async def grant_pass(
    body: GrantPassIn,
    user: User = Depends(get_user),
    session: AsyncSession = Depends(get_session),
):
    """Give or extend a pass by hand. Plan §12, and the first thing you want when a
    Tribute webhook is missed.

    **No `Purchase` row is written.** Purchases are money: they drive revenue reporting
    and they are what a refund is matched against (§4.1), so inventing one for a grant
    would corrupt both. `pass_expires_at` is set directly and a distinct event type
    records it, which also keeps it out of the conversion funnel — a granted pass is not
    someone deciding to pay.

    Extending is additive from whichever is later, so granting twice to an active pass
    adds time rather than shortening it to `days` from today.
    """
    now = datetime.now(timezone.utc)
    base = user.pass_expires_at if (
        user.pass_expires_at and user.pass_expires_at > now
    ) else now
    user.pass_expires_at = base + timedelta(days=body.days)

    await record(
        session,
        EV_PASS_GRANTED,
        chat_id=user.chat_id,
        days=body.days,
        reason=body.reason,
        expires_at=user.pass_expires_at.isoformat(),
    )
    await session.flush()
    return await _out(user, session)


@router.delete("/{chat_id}", status_code=204)
async def erase(chat_id: int, session: AsyncSession = Depends(get_session)):
    """GDPR erasure. Idempotent: deleting an unknown user is still a 204."""
    await users.delete_user(session, chat_id)
    return Response(status_code=204)
