"""The owner's console, inside the Mini App.

Payment is a direct arrangement now: somebody messages the owner, they agree terms, and
access is granted by hand. That needs a place to grant it from, links to hand out trials
with, and a way to reach people. This is that place.

WHY IT LIVES HERE AND NOT ON ITS OWN DOMAIN

An admin panel can give away paid access, message every user, and read personal data. It is
the most dangerous surface in the product, and this API's founding rule is that it has NO
authentication and must never be publicly reachable — see the module docstring in
api/routes/webapp.py.

Putting it in the Mini App reuses the one authenticated path that already exists: every
request carries a Telegram-signed `initData` blob whose HMAC is checked before anything
else runs. No new public surface, no new front door, and no authentication code written for
this feature standing between the internet and the ability to hand out Premium.

`staff_user` is the whole of the authorisation model, it is a dependency on EVERY route
here, and a route added later without it is a route anyone can call.

IT ANSWERS 404, NOT 403

A 403 confirms the endpoint exists and that the caller merely lacks the rank. To anything
probing this surface, an admin panel that is present but forbidden is an invitation; one
that is indistinguishable from a typo is not.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import Event, Purchase, ReferralLink, User
from api.routes.webapp import webapp_user
from api.services import broadcast, events, notify, referrals
from api.services.entitlement import evaluate
from api.services.users import _clean_source
from shared.constants import (
    EV_BROADCAST_SENT,
    EV_LINK_CHANGED,
    EV_PASS_GRANTED,
    UI_LANGUAGES,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webapp/admin", tags=["admin"])

# The longest a single hand-grant may run. A cap on a slipped zero: ten years of Premium
# given away by a typo is not recoverable once somebody has it.
MAX_GRANT_DAYS = 400


async def staff_user(user: User = Depends(webapp_user)) -> User:
    """The whole authorisation model. Every route in this file depends on it.

    404 rather than 403 on purpose — see the module docstring.
    """
    if not evaluate(user).is_staff:
        log.warning("non-staff %s probed the admin surface", user.chat_id)
        raise HTTPException(404, "not found")
    return user


# --- what is going on --------------------------------------------------------

@router.get("/overview")
async def overview(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    now = datetime.now(timezone.utc)
    total = await session.scalar(select(func.count(User.chat_id)))
    premium = await session.scalar(
        select(func.count(User.chat_id)).where(User.pass_expires_at > now))
    trials = await session.scalar(
        select(func.count(User.chat_id))
        .where(User.pass_expires_at > now, User.source.is_not(None)))
    paid = await session.scalar(
        select(func.count(Purchase.id)).where(Purchase.amount_cents > 0))
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Purchase.amount_cents), 0))
        .where(Purchase.amount_cents > 0, Purchase.refunded_at.is_(None)))
    active_today = await session.scalar(
        select(func.count(func.distinct(Event.chat_id)))
        .where(Event.created_at > now - timedelta(days=1)))
    return {
        "users": total or 0,
        "premium": premium or 0,
        "on_trial": trials or 0,
        "paid_purchases": paid or 0,
        "revenue_cents": revenue or 0,
        "active_24h": active_today or 0,
        "sales_contact": __import__("shared.config", fromlist=["settings"]).settings.sales_handle,
    }


@router.get("/users")
async def list_users(
    q: str = Query(default="", max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Find somebody, by chat id, name or referral code.

    Newest first: the person the owner is looking for has almost always just messaged them.
    """
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    if q:
        term = f"%{q}%"
        conditions = [User.display_name.ilike(term), User.source.ilike(term)]
        if q.lstrip("-").isdigit():
            conditions.append(User.chat_id == int(q))
        stmt = stmt.where(or_(*conditions))

    now = datetime.now(timezone.utc)
    rows = list(await session.scalars(stmt))
    return {
        "users": [
            {
                "chat_id": u.chat_id,
                "name": u.display_name,
                "lang": u.lang,
                "premium": bool(u.pass_expires_at and u.pass_expires_at > now),
                "pass_expires_at": u.pass_expires_at,
                "source": u.source,
                "created_at": u.created_at,
            }
            for u in rows
        ]
    }


# --- giving access -----------------------------------------------------------

class GrantIn(BaseModel):
    days: int = Field(ge=1, le=MAX_GRANT_DAYS)
    reason: str = Field(default="", max_length=200)
    # Whether to tell them. Off by default: the owner is usually mid-conversation with this
    # person and a bot message arriving on top of their own reply is noise.
    notify: bool = False

    # What they actually paid, in cents. THIS IS NOT BOOKKEEPING GARNISH.
    #
    # A pass with no money behind it is how the app decides somebody is on a TRIAL: `plan()`
    # branches on `purchased`, which is a Purchase row. Payment moved to hand-grants on
    # 2026-08-09 and nothing wrote one, so every paying customer was shown "Free trial — 30
    # days left. Nothing will be charged." after paying, and every revenue figure read zero
    # for ever.
    #
    # 0 means genuinely free — a comp, a tester, an apology — and still produces the trial
    # wording, which is correct for those.
    amount_cents: int = Field(default=0, ge=0, le=100_000)
    currency: str = Field(default="EUR", max_length=8)


@router.post("/users/{chat_id}/grant")
async def grant(
    chat_id: int,
    body: GrantIn,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Extend somebody's access by hand. This is how the product is sold now.

    EXTENDS rather than replaces. Somebody paying for a second month while the first is
    still running must not lose the remainder — granting from `now` would silently shorten
    what they already had.
    """
    user = await session.get(User, chat_id)
    if user is None:
        raise HTTPException(404, "no such user")

    now = datetime.now(timezone.utc)
    base = user.pass_expires_at if (user.pass_expires_at and user.pass_expires_at > now) else now
    previous = user.pass_expires_at
    user.pass_expires_at = base + timedelta(days=body.days)

    if body.amount_cents > 0:
        # A real sale. The row is what makes `purchased` true — so the buyer is told their
        # subscription is active rather than that they are on a free trial — and it is what
        # every revenue figure counts.
        #
        # `tribute_purchase_id` is UNIQUE and NOT NULL, and there is no Tribute any more, so
        # it carries a synthetic id instead. Keeping the column rather than renaming it: the
        # refund matcher and the idempotency guarantee are both built on it, and a rename is
        # a migration plus four call sites for a cosmetic gain.
        session.add(Purchase(
            chat_id=chat_id,
            # Readable AND collision-free. A whole-second timestamp alone was neither:
            # two sales to the same person in the same second — a double-tap, or selling a
            # top-up right after a renewal — violated the UNIQUE constraint and failed the
            # second sale. Caught by its own test.
            tribute_purchase_id=(
                f"manual:{chat_id}:{now:%Y%m%d%H%M%S}:{uuid4().hex[:8]}"),
            tier=f"manual_{body.days}d",
            amount_cents=body.amount_cents,
            currency=body.currency.upper()[:8],
            extended_from=previous,
            extended_to=user.pass_expires_at,
        ))

    await events.record(
        session, EV_PASS_GRANTED, chat_id=chat_id,
        days=body.days, reason=body.reason[:200], by=staff.chat_id,
        amount_cents=body.amount_cents, currency=body.currency.upper()[:8],
        expires_at=user.pass_expires_at.isoformat(),
    )
    await session.commit()

    if body.notify:
        await notify.payment(chat_id, user.lang, "paid", user.pass_expires_at, "")

    return {"chat_id": chat_id, "pass_expires_at": user.pass_expires_at}


# --- referral links ----------------------------------------------------------

class LinkIn(BaseModel):
    code: str = Field(min_length=2, max_length=64)
    label: str = Field(default="", max_length=120)
    trial_days: int = Field(default=7, ge=1, le=referrals.MAX_TRIAL_DAYS)
    max_uses: int | None = Field(default=None, ge=1)


class LinkPatch(BaseModel):
    active: bool | None = None
    label: str | None = Field(default=None, max_length=120)
    trial_days: int | None = Field(default=None, ge=1, le=referrals.MAX_TRIAL_DAYS)
    max_uses: int | None = Field(default=None, ge=1)


@router.get("/links")
async def list_links(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    from shared.config import settings

    rows = list(await session.scalars(
        select(ReferralLink).order_by(ReferralLink.created_at.desc())))
    bot = settings.bot_username.lstrip("@")
    return {
        "links": [
            {
                "code": r.code,
                "label": r.label,
                "trial_days": r.trial_days,
                "active": r.active,
                "max_uses": r.max_uses,
                "uses": await referrals.uses(session, r.code),
                "url": f"https://t.me/{bot}?start={r.code}" if bot else "",
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.post("/links", status_code=201)
async def create_link(
    body: LinkIn,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    # Through the SAME cleaner the /start payload goes through. A code that survives here
    # but not there would be a link that cannot grant what it promises.
    code = _clean_source(body.code)
    if not code:
        raise HTTPException(422, "a code must be letters, digits, - or _")
    if await session.get(ReferralLink, code) is not None:
        raise HTTPException(409, "that code already exists")

    session.add(ReferralLink(
        code=code, label=body.label, trial_days=body.trial_days,
        max_uses=body.max_uses, created_by=staff.chat_id,
    ))
    await events.record(session, EV_LINK_CHANGED, chat_id=staff.chat_id,
                        code=code, action="created", trial_days=body.trial_days)
    await session.commit()
    return {"code": code}


@router.patch("/links/{code}")
async def update_link(
    code: str,
    body: LinkPatch,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Switch a link off, or change what it is worth.

    There is no delete. Deleting a link would erase the attribution of every user it ever
    brought, and `active=False` already stops it granting anything.
    """
    link = await session.get(ReferralLink, code)
    if link is None:
        raise HTTPException(404, "no such link")

    for field in ("active", "label", "trial_days", "max_uses"):
        value = getattr(body, field)
        if value is not None:
            setattr(link, field, value)

    await events.record(session, EV_LINK_CHANGED, chat_id=staff.chat_id,
                        code=code, action="updated", active=link.active)
    await session.commit()
    return {"code": code, "active": link.active}


# --- reaching people ---------------------------------------------------------

class MessageIn(BaseModel):
    chat_id: int
    text: str = Field(min_length=1, max_length=3500)


@router.post("/message")
async def message_one(
    body: MessageIn,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """A private message to one learner — the reply to "how do I pay?".

    Sent inline rather than in the background: it is one message, the owner is waiting for
    it, and whether it arrived is the whole point.
    """
    if await session.get(User, body.chat_id) is None:
        raise HTTPException(404, "no such user")
    delivered = await notify.send(body.chat_id, body.text)
    await events.record(session, EV_BROADCAST_SENT, chat_id=staff.chat_id,
                        recipients=1, delivered=int(delivered),
                        failed=int(not delivered), label="direct",
                        preview=body.text[:200])
    await session.commit()
    return {"delivered": delivered}


class BroadcastIn(BaseModel):
    text: str = Field(min_length=1, max_length=3500)
    lang: str | None = None
    premium_only: bool = False
    label: str = Field(default="", max_length=80)
    # Refuse to send unless the caller has seen the count. A newsletter cannot be unsent,
    # and the number of people it is about to reach is the one fact worth confirming.
    confirm_recipients: int | None = None


@router.post("/broadcast/preview")
async def preview_broadcast(
    body: BroadcastIn,
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    if body.lang and body.lang not in UI_LANGUAGES:
        raise HTTPException(422, f"lang must be one of {UI_LANGUAGES}")
    ids = await broadcast.recipients(
        session, lang=body.lang, premium_only=body.premium_only)
    return {"recipients": len(ids), "capped_at": broadcast.MAX_RECIPIENTS}


@router.post("/broadcast")
async def send_broadcast(
    body: BroadcastIn,
    background: BackgroundTasks,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Send to everyone matching the filter. Runs detached; the result goes to the log.

    `confirm_recipients` must match what /preview just reported. That is not ceremony: the
    filter and the send are two requests, the population can change between them, and a
    newsletter cannot be unsent. A mismatch means the caller is confirming a number they
    were not shown.
    """
    if body.lang and body.lang not in UI_LANGUAGES:
        raise HTTPException(422, f"lang must be one of {UI_LANGUAGES}")

    ids = await broadcast.recipients(
        session, lang=body.lang, premium_only=body.premium_only)
    if not ids:
        raise HTTPException(409, "nobody matches that filter")
    if body.confirm_recipients is None or body.confirm_recipients != len(ids):
        raise HTTPException(
            409,
            f"confirm {len(ids)} recipients — you sent {body.confirm_recipients}",
        )

    background.add_task(broadcast.send_many, ids, body.text,
                        sent_by=staff.chat_id, label=body.label)
    return {"queued": len(ids)}


@router.get("/broadcast/history")
async def broadcast_history(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    return {"sent": await broadcast.history(session)}
