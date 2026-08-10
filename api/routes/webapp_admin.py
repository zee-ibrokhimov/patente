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

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from api.models import (
    Cluster, Event, Explanation, Purchase, Question, QuizSession, ReferralLink,
    Report, Translation, User,
)
from api.routes.webapp import PREFETCH_CONCURRENCY, webapp_user
from api.services import broadcast, events, explanations, lapse, notify, referrals
from api.services.entitlement import evaluate
from api.services.users import _clean_source
from shared.constants import (
    EV_ANSWER_GIVEN,
    EV_BROADCAST_SENT,
    EV_LINK_CHANGED,
    EV_PASS_ENDING,
    EV_PASS_GRANTED,
    EV_PASS_LAPSED,
    EV_PASS_REVOKED,
    EV_REPORT_RESOLVED,
    EV_USER_DELETED,
    EV_MODEL_CALL,
    MANUAL_PURCHASE_PREFIX,
    MODE_EXAM,
    SERVABLE_STATUSES,
    STATUS_FLAGGED,
    USER_ACTIVITY_EVENTS,
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
    # USER_ACTIVITY_EVENTS, not every row with a chat_id on it. This counted all event
    # types, so the owner sending a newsletter or granting a pass registered as active
    # users — the one number here meant to say "is anybody still here" was inflated by the
    # system's own writes. Same defect that made the reminder gap unreachable.
    active_today = await session.scalar(
        select(func.count(func.distinct(Event.chat_id)))
        .where(Event.created_at > now - timedelta(days=1),
               Event.type.in_(USER_ACTIVITY_EVENTS)))

    # How much of the bank is actually written.
    #
    # Both are generated on demand, so the bank fills in whatever order a handful of users
    # happen to wander through it — and nothing anywhere reported how far that had got. The
    # numbers are small and surprising (translations were at 3.6% of 7,106 when this was
    # added), which is exactly why they belong on the screen the owner opens rather than in
    # a query he would have to know to run.
    #
    # DISTINCT question / cluster, not row counts: a translated question holds one row per
    # language, so counting rows reports three times the coverage.
    questions_total = await session.scalar(select(func.count(Question.id)))
    translated = await session.scalar(
        select(func.count(func.distinct(Translation.question_id))))
    clusters_total = await session.scalar(select(func.count(Cluster.id)))
    explained = await session.scalar(
        select(func.count(func.distinct(Explanation.cluster_id)))
        .where(Explanation.status.in_(SERVABLE_STATUSES)))
    # Written but withheld — a gate caught a number it could not ground, or the model
    # argued against the answer key. Worth its own figure: it is the only visible measure
    # of how often generation produces something untrustworthy.
    withheld = await session.scalar(
        select(func.count(func.distinct(Explanation.cluster_id)))
        .where(Explanation.status == STATUS_FLAGGED))
    # DISTINCT cluster, like the two counts above it. This counted rows and reported 180
    # where the truth was 45 — one row per language, so exactly four times too many, on the
    # same screen as two counters that had just been fixed for that. A row count is never
    # the answer here: an explanation is one thing written in four languages.
    disputed = await session.scalar(
        select(func.count(func.distinct(Explanation.cluster_id)))
        .where(Explanation.disputed.is_not(None), Explanation.disputed != ""))

    # How many QUESTIONS those explanations actually cover.
    #
    # "100 of 3,382 rules · 3.0%" was true and misleading. An explanation is per CLUSTER,
    # clusters are wildly uneven — the biggest covers 34 questions, the median two — so 100
    # of them already answer 1,157 questions, 16% of the bank rather than 3%. Reporting only
    # the rule count understates the real position by five times and makes deliberate
    # generation look pointless.
    covered = await session.scalar(
        select(func.count(Question.id)).where(
            Question.cluster_id.in_(
                select(Explanation.cluster_id)
                .where(Explanation.status.in_(SERVABLE_STATUSES)))))

    return {
        "content": {
            "questions_total": questions_total or 0,
            "translated": translated or 0,
            "clusters_total": clusters_total or 0,
            "explained": explained or 0,
            "questions_covered": covered or 0,
            "explanations_withheld": withheld or 0,
            "explanations_disputed": disputed or 0,
        },
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
    segment: str = Query(default=""),
    within_days: int = Query(default=7, ge=1, le=90),
    limit: int = Query(default=50, ge=1, le=200),
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Find somebody, by chat id, name or referral code.

    Newest first: the person the owner is looking for has almost always just messaged them.
    """
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)

    # The same four segments the group-grant targets, now something you can LOOK at.
    # They existed only as a destination for free days: "who runs out this week so I can
    # ask them to renew" — the conversation that actually produces money — had no screen.
    # `_segment_ids` is reused rather than reimplemented, so the list and the grant can
    # never disagree about who is in a segment.
    if segment:
        ids = await _segment_ids(session, segment, within_days)
        if not ids:
            return {"users": [], "segment": segment}
        stmt = stmt.where(User.chat_id.in_(ids))
        # Soonest-to-expire first for the two segments that are about a deadline; the
        # default newest-first is useless when the question is "who is running out".
        if segment in ("expiring", "lapsed"):
            stmt = stmt.order_by(None).order_by(User.pass_expires_at.asc()).limit(limit)

    if q:
        term = f"%{q}%"
        conditions = [User.display_name.ilike(term), User.source.ilike(term)]
        if q.lstrip("-").isdigit():
            conditions.append(User.chat_id == int(q))
        stmt = stmt.where(or_(*conditions))

    now = datetime.now(timezone.utc)
    rows = list(await session.scalars(stmt))

    # When each of them was last seen, in one query rather than one per row.
    #
    # From the event log, because there is no `last_seen` column and adding one would be a
    # migration to store something already recorded. Without this the panel cannot answer
    # the question retention starts from — who is slipping away — and a row said only
    # "123456 · ru · PREMIUM", which is true of somebody who left a month ago.
    seen: dict[int, datetime] = {}
    if rows:
        for chat_id, last in await session.execute(
            select(Event.chat_id, func.max(Event.created_at))
            .where(Event.chat_id.in_([u.chat_id for u in rows]))
            .group_by(Event.chat_id)
        ):
            seen[chat_id] = last

    # How many the filter matches, not how many are being returned. The card shows this
    # instead of a list, so it has to be the real total rather than min(total, limit).
    matching = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).limit(None).subquery()))

    return {
        "segment": segment,
        "total": matching or 0,
        "users": [
            {
                "chat_id": u.chat_id,
                "name": u.display_name,
                "lang": u.lang,
                "premium": bool(u.pass_expires_at and u.pass_expires_at > now),
                "pass_expires_at": u.pass_expires_at,
                "source": u.source,
                "created_at": u.created_at,
                "last_seen": seen.get(u.chat_id),
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
                f"{MANUAL_PURCHASE_PREFIX}{chat_id}:{now:%Y%m%d%H%M%S}:"
                f"{uuid4().hex[:8]}"),
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
    # WHO, using the same segments as the group grant and the user list.
    #
    # Until now a newsletter could be filtered by language and by subscribers-only and
    # nothing else, so "your access ends Friday, message me to extend" had to go to
    # everybody or to nobody — the renewal ask had no audience. Reuses `_segment_ids`
    # rather than growing a second definition of "expiring" that can drift from the first.
    segment: str = Field(default="")
    within_days: int = Field(default=7, ge=1, le=90)
    label: str = Field(default="", max_length=80)
    # Refuse to send unless the caller has seen the count. A newsletter cannot be unsent,
    # and the number of people it is about to reach is the one fact worth confirming.
    confirm_recipients: int | None = None

    # An image at the top of the newsletter. A URL rather than an upload: Telegram fetches
    # it itself, so there is no file to store, serve or back up, and the ad artwork already
    # lives somewhere public. Note that a photo caption is capped at 1024 characters — over
    # that, send_rich drops the image and sends the full text.
    photo_url: str | None = None

    # Up to three inline buttons under the message. The interesting one is
    # {"text": "...", "webapp": true}, which opens the Mini App in place — that is what
    # turns "special offer" into one tap onto the paywall rather than a trip to a browser.
    # Validated by broadcast.buttons_for, not trusted.
    buttons: list[dict] | None = None


async def _audience(session: AsyncSession, body: BroadcastIn) -> list[int]:
    """Who a newsletter reaches: the language/premium filter, narrowed by a segment.

    Intersected rather than replaced, so "expiring, in Russian" is expressible. Both halves
    already existed and neither could see the other.
    """
    ids = await broadcast.recipients(
        session, lang=body.lang, premium_only=body.premium_only)
    if not body.segment:
        return ids
    inside = set(await _segment_ids(session, body.segment, body.within_days))
    return [chat_id for chat_id in ids if chat_id in inside]


@router.post("/broadcast/preview")
async def preview_broadcast(
    body: BroadcastIn,
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    if body.lang and body.lang not in UI_LANGUAGES:
        raise HTTPException(422, f"lang must be one of {UI_LANGUAGES}")
    ids = await _audience(session, body)
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

    ids = await _audience(session, body)
    if not ids:
        raise HTTPException(409, "nobody matches that filter")
    if body.confirm_recipients is None or body.confirm_recipients != len(ids):
        raise HTTPException(
            409,
            f"confirm {len(ids)} recipients — you sent {body.confirm_recipients}",
        )

    # Validated BEFORE queueing, so a bad button is a 422 the sender sees rather than a
    # silent failure discovered after the newsletter has already gone to everyone.
    try:
        buttons = broadcast.buttons_for(body.buttons)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    background.add_task(broadcast.send_many, ids, body.text,
                        sent_by=staff.chat_id, label=body.label,
                        photo_url=body.photo_url, buttons=buttons)
    return {"queued": len(ids), "buttons": len(buttons), "photo": bool(body.photo_url)}


@router.get("/broadcast/history")
async def broadcast_history(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    return {"sent": await broadcast.history(session)}


# --- what learners are telling you ------------------------------------------

@router.get("/reports")
async def list_reports(
    unresolved: bool = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """"This explanation is wrong", with enough context to judge it without leaving the page.

    The button that files these has shipped for a long time and NOTHING read the table, so
    the complaints accumulated where no one could see them. That is the worst possible state
    for this particular feature: it invites a learner to tell you the app is wrong, and then
    discards what they said. Every report here is a person who cared enough to tap.

    The explanation text is joined in deliberately. A report is only actionable next to the
    sentence being reported, and having to look the cluster up by hand is exactly the
    friction that leaves a queue unread.
    """
    stmt = (
        select(Report, Question.statement_it, Question.cluster_id)
        .join(Question, Question.id == Report.question_id)
        .order_by(Report.created_at.desc())
        .limit(limit)
    )
    if unresolved:
        stmt = stmt.where(Report.resolved_at.is_(None))

    rows = (await session.execute(stmt)).all()

    out = []
    for report, statement, cluster_id in rows:
        text = None
        if cluster_id is not None:
            explanation = await session.scalar(
                select(Explanation).where(
                    Explanation.cluster_id == cluster_id,
                    Explanation.lang == report.lang,
                )
            )
            text = explanation.text if explanation else None
        out.append({
            "id": report.id,
            "chat_id": report.chat_id,
            "question_id": report.question_id,
            "cluster_id": cluster_id,
            "lang": report.lang,
            "statement": statement,
            "explanation": text,
            "created_at": report.created_at,
            "resolved_at": report.resolved_at,
        })

    open_count = await session.scalar(
        select(func.count(Report.id)).where(Report.resolved_at.is_(None))
    )
    return {"reports": out, "open": open_count or 0}


@router.post("/reports/{report_id}/resolve")
async def resolve_report(
    report_id: int,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Mark one report as dealt with. Idempotent: resolving twice is not an error, because
    the realistic way this is used is two taps on a slow connection."""
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "no such report")
    if report.resolved_at is None:
        report.resolved_at = datetime.now(timezone.utc)
        await events.record(session, EV_REPORT_RESOLVED, chat_id=staff.chat_id,
                            report_id=report_id, question_id=report.question_id)
        await session.commit()
    return {"id": report_id, "resolved_at": report.resolved_at}


@router.post("/reports/{report_id}/regenerate")
async def regenerate_reported(
    report_id: int,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Rewrite the explanation a learner reported, and resolve the report.

    The whole point of reading the queue is being able to act on it in the same place. This
    deletes the stored rows for the cluster and regenerates them, so the next reader gets a
    fresh answer rather than the one that was just complained about.

    Runs in the FOREGROUND, unlike the prefetch: staff tapped it and are waiting to see
    whether the new text is better, and a background rewrite would leave them looking at
    the old one with no way to tell if anything happened.
    """
    report = await session.get(Report, report_id)
    if report is None:
        raise HTTPException(404, "no such report")
    if report.cluster_id is None:
        raise HTTPException(409, "that report is not attached to a cluster")

    await session.execute(
        sa_delete(Explanation).where(Explanation.cluster_id == report.cluster_id)
    )
    await session.commit()

    outcome = await explanations.generate(session, report.cluster_id)
    if report.resolved_at is None:
        report.resolved_at = datetime.now(timezone.utc)
    await events.record(session, EV_REPORT_RESOLVED, chat_id=staff.chat_id,
                        report_id=report_id, action="regenerated",
                        outcome=outcome.outcome)
    await session.commit()

    fresh = await session.scalar(
        select(Explanation).where(
            Explanation.cluster_id == report.cluster_id,
            Explanation.lang == report.lang,
        )
    )
    return {
        "id": report_id,
        "outcome": outcome.outcome,
        "explanation": fresh.text if fresh else None,
        "status": fresh.status if fresh else None,
    }


# --- removing things ---------------------------------------------------------

@router.delete("/users/{chat_id}")
async def delete_user(
    chat_id: int,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Remove a learner and everything of theirs.

    Irreversible, and the only destructive operation in this panel. Two guards:

      · Staff cannot delete themselves. Deleting the only staff account locks the panel
        for good, and there is no second way in — `staff_user` is the only gate.
      · The PURCHASES are kept, orphaned, on purpose. They are the record of money that
        changed hands; a refund request or a tax question outlives the account, and
        deleting the row does not undo the payment. Everything else — progress, sessions,
        answers, vocabulary, reports — belongs to the person and goes.

    The event line survives too, so "where did this account go" is answerable afterwards.
    """
    if chat_id == staff.chat_id:
        raise HTTPException(409, "you cannot delete your own staff account")

    user = await session.get(User, chat_id)
    if user is None:
        raise HTTPException(404, "no such user")

    kept = await session.scalar(
        select(func.count(Purchase.id)).where(Purchase.chat_id == chat_id)
    )
    await events.record(session, EV_USER_DELETED, chat_id=staff.chat_id,
                        deleted=chat_id, purchases_kept=kept or 0)
    await session.delete(user)
    await session.commit()
    return {"deleted": chat_id, "purchases_kept": kept or 0}


@router.delete("/links/{code}")
async def delete_link(
    code: str,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a referral link outright.

    `PATCH active=false` remains the right tool almost always, and the reason the delete did
    not exist is still true: the code is how every user it brought is attributed, and
    removing it makes that history unreadable. So this refuses once the link has been USED,
    and points at deactivating instead. An unused link is just a typo, and having to live
    with a typo for ever is its own kind of bad.
    """
    link = await session.get(ReferralLink, code)
    if link is None:
        raise HTTPException(404, "no such link")

    used = await referrals.uses(session, code)
    if used:
        raise HTTPException(
            409,
            f"{used} user(s) came from this link — deactivate it instead of deleting, "
            f"or their attribution is lost",
        )

    await events.record(session, EV_LINK_CHANGED, chat_id=staff.chat_id,
                        code=code, action="deleted")
    await session.delete(link)
    await session.commit()
    return {"deleted": code}


# --- granting to more than one person at a time ------------------------------

# A ceiling on a segment grant. MAX_GRANT_DAYS already caps how LONG one grant can be; this
# caps how MANY it can reach, which is the other way a slip becomes unrecoverable — access
# cannot be taken back from someone who has already been told they have it.
MAX_GRANT_RECIPIENTS = 500

SEGMENTS = ("active", "expiring", "lapsed", "trial", "quiet")


class GrantManyIn(BaseModel):
    """Give the same number of days to everyone in a segment."""

    segment: str
    days: int = Field(ge=1, le=MAX_GRANT_DAYS)
    # What "active" and "expiring" measure against. 7 for active means "used it this week".
    within_days: int = Field(default=7, ge=1, le=90)
    reason: str = Field(default="", max_length=200)
    notify: bool = True
    # Same guard as the newsletter, for the same reason: the segment is computed twice, the
    # population moves between the two calls, and a grant cannot be taken back.
    confirm_recipients: int | None = None


async def _segment_ids(session: AsyncSession, segment: str, within_days: int) -> list[int]:
    """Who a segment contains, in a stable order.

    ACTIVE is measured from the event log rather than from a `last_seen` column, because no
    such column exists and adding one would be a migration to store something already
    recorded. Any event counts — answering, starting a sitting, opening a screen — which is
    the honest reading of "using my project".
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=within_days)

    if segment == "active":
        stmt = (select(func.distinct(Event.chat_id))
                .where(Event.created_at > since,
                       Event.type.in_(USER_ACTIVITY_EVENTS)))
    elif segment == "expiring":
        # Still paid up, but not for long. This is the renewal conversation.
        stmt = (select(User.chat_id)
                .where(User.pass_expires_at > now,
                       User.pass_expires_at <= now + timedelta(days=within_days)))
    elif segment == "lapsed":
        stmt = (select(User.chat_id)
                .where(User.pass_expires_at.is_not(None), User.pass_expires_at <= now))
    elif segment == "quiet":
        # Nobody has heard from them. The mirror image of `active`, and the segment
        # retention starts from — measured the same way, from the event log, so the two
        # can never disagree about what "using the app" means.
        seen = select(func.distinct(Event.chat_id)).where(
            Event.created_at > since, Event.type.in_(USER_ACTIVITY_EVENTS))
        stmt = select(User.chat_id).where(User.chat_id.not_in(seen))
    elif segment == "trial":
        # Access with no money behind it: a referral trial or a previous gift. `purchased`
        # is computed the same way everywhere — a Purchase row with amount_cents > 0.
        paid = select(Purchase.chat_id).where(Purchase.amount_cents > 0)
        stmt = (select(User.chat_id)
                .where(User.pass_expires_at > now, User.chat_id.not_in(paid)))
    else:
        raise HTTPException(422, f"segment must be one of {SEGMENTS}")

    return sorted(await session.scalars(stmt.order_by(None)))


@router.post("/grant-many/preview")
async def preview_grant_many(
    body: GrantManyIn,
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    ids = await _segment_ids(session, body.segment, body.within_days)
    return {"recipients": len(ids), "capped_at": MAX_GRANT_RECIPIENTS}


@router.post("/grant-many")
async def grant_many(
    body: GrantManyIn,
    background: BackgroundTasks,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Give days to everyone in a segment — the way to reward people who actually use it.

    EXTENDS each person's own expiry, exactly as the single grant does, so somebody who
    already paid for a month does not have it silently shortened to the gift.

    No Purchase row is written. These are gifts, not sales: counting them as revenue would
    make every figure in the overview wrong, and marking the recipients `purchased` would
    tell someone who was given a week that they have an active paid subscription.

    Notifications go out in the BACKGROUND while the grant itself commits first. The access
    is the thing that must not fail; being told about it can lag or fail per person, and
    Telegram rate limits mean 500 messages take half a minute.
    """
    ids = await _segment_ids(session, body.segment, body.within_days)
    if not ids:
        raise HTTPException(409, "nobody is in that segment")
    if len(ids) > MAX_GRANT_RECIPIENTS:
        raise HTTPException(
            409, f"{len(ids)} people — more than the {MAX_GRANT_RECIPIENTS} cap")
    if body.confirm_recipients is None or body.confirm_recipients != len(ids):
        raise HTTPException(
            409, f"confirm {len(ids)} recipients — you sent {body.confirm_recipients}")

    now = datetime.now(timezone.utc)
    granted: list[tuple[int, str, datetime]] = []
    for chat_id in ids:
        user = await session.get(User, chat_id)
        if user is None:
            continue                      # an event from an account since deleted
        base = (user.pass_expires_at
                if user.pass_expires_at and user.pass_expires_at > now else now)
        user.pass_expires_at = base + timedelta(days=body.days)
        granted.append((chat_id, user.lang, user.pass_expires_at))

    await events.record(
        session, EV_PASS_GRANTED, chat_id=staff.chat_id,
        segment=body.segment, within_days=body.within_days,
        days=body.days, recipients=len(granted), reason=body.reason[:200],
        bulk=True,
    )
    await session.commit()

    if body.notify:
        background.add_task(_tell_them, granted)

    return {"granted": len(granted), "segment": body.segment, "days": body.days}


async def _tell_them(granted: list[tuple[int, str, datetime]]) -> None:
    """Tell each recipient, slowly enough not to be rate-limited. Never raises."""
    for chat_id, lang, expires_at in granted:
        try:
            await notify.payment(chat_id, lang, "paid", expires_at, "")
        except Exception:                                             # noqa: BLE001
            log.warning("could not tell %s about their grant", chat_id, exc_info=True)
        await asyncio.sleep(broadcast.PAUSE)


# --- taking access back ------------------------------------------------------

class RevokeIn(BaseModel):
    """End a pass now, or shorten it by a number of days."""

    mode: str = Field(default="end")           # "end" | "shorten"
    days: int = Field(default=0, ge=0, le=MAX_GRANT_DAYS)


@router.post("/users/{chat_id}/revoke")
async def revoke(
    chat_id: int,
    body: RevokeIn,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Take access back — the only way down that does not destroy the learner.

    Everything else in this panel moves access UP. `grant` is `days >= 1`, `grant-many`
    only ever adds, and until now the sole correction for a slipped digit or a gift sent to
    the wrong segment was DELETE /users/{chat_id}, which erases somebody's progress,
    sessions and answers to fix a date. The group-grant confirm says the quiet part out
    loud: "Access cannot be taken back."

    SET TO `now`, NEVER TO NULL. The two behave identically for the `premium` count and the
    `trial` segment, which both filter `pass_expires_at > now` — but `lapse.py` and the
    `lapsed` segment filter `pass_expires_at IS NOT NULL`, so NULL would quietly erase the
    fact that this person ever had access at all.

    AND SUPPRESS THE LAPSE NOTICE. Setting the expiry to `now` drops the user straight into
    lapse.py's ended-window (`pass_expires_at <= now` within LOOK_BACK), so the next cron
    run would message them "your Premium has ended", pointing at renewal — for a gift the
    owner had just quietly taken back, usually mid-conversation with them. `_already_told`
    matches on (chat_id, type, expires_at), so recording the announcement here, without
    sending it, is exactly what stops it. Same for the 3-day ending warning when a shortened
    pass lands inside that window.

    Purchases are untouched. `refunded_at` belongs to the Tribute refund matcher, and giving
    money back is a separate act from ending access.
    """
    user = await session.get(User, chat_id)
    if user is None:
        raise HTTPException(404, "no such user")

    now = datetime.now(timezone.utc)
    previous = user.pass_expires_at
    if previous is None or previous <= now:
        raise HTTPException(409, "they have no access to take back")

    if body.mode == "end":
        user.pass_expires_at = now
    elif body.mode == "shorten":
        if body.days < 1:
            raise HTTPException(422, "shorten by how many days?")
        reduced = previous - timedelta(days=body.days)
        user.pass_expires_at = reduced if reduced > now else now
    else:
        raise HTTPException(422, 'mode must be "end" or "shorten"')

    stamp = user.pass_expires_at.isoformat()
    await events.record(session, EV_PASS_REVOKED, chat_id=chat_id,
                        by=staff.chat_id, mode=body.mode, days=body.days,
                        previous=previous.isoformat(), expires_at=stamp)

    # Pre-record whichever notice this new expiry would otherwise trigger, so the cron
    # sees it as already announced and stays quiet.
    if user.pass_expires_at <= now:
        await events.record(session, EV_PASS_LAPSED, chat_id=chat_id, expires_at=stamp)
    elif user.pass_expires_at <= now + lapse.WARN_BEFORE:
        await events.record(session, EV_PASS_ENDING, chat_id=chat_id, expires_at=stamp)

    await session.commit()
    return {
        "chat_id": chat_id,
        "pass_expires_at": user.pass_expires_at,
        "previous": previous,
    }


# --- one learner, in full ----------------------------------------------------

@router.get("/users/{chat_id}")
async def one_user(
    chat_id: int,
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Everything about one person, for the moment they message "I paid" or "it's broken".

    The list gave one line — `123456 · ru · PREMIUM` — so the answer to "did Aziz's 10.99
    ever arrive" was either memory or asking the customer, which is the question you least
    want to ask the person who just paid you.

    Nothing here is new data. It is all recorded already and none of it was reachable.
    """
    user = await session.get(User, chat_id)
    if user is None:
        raise HTTPException(404, "no such user")

    now = datetime.now(timezone.utc)
    ent = evaluate(user)

    payments = [
        {
            "id": p.id,
            "amount_cents": p.amount_cents,
            "currency": p.currency,
            "tier": p.tier,
            "created_at": p.created_at,
            "refunded_at": p.refunded_at,
            # Which of the two kinds. A hand sale and a Tribute subscription behave
            # differently at renewal, and only the id says which this was.
            "manual": (p.tribute_purchase_id or "").startswith(MANUAL_PURCHASE_PREFIX),
        }
        for p in await session.scalars(
            select(Purchase).where(Purchase.chat_id == chat_id)
            .order_by(Purchase.created_at.desc()).limit(20))
    ]

    last_seen = await session.scalar(
        select(func.max(Event.created_at)).where(Event.chat_id == chat_id))
    answers = await session.scalar(
        select(func.count(Event.id))
        .where(Event.chat_id == chat_id, Event.type == EV_ANSWER_GIVEN))
    exams = await session.scalar(
        select(func.count(QuizSession.id))
        # `passed is not None` so the owner's count means what the learner's "Exams taken"
        # means. Without it this counted every started-and-left sitting, so the console
        # reported more exams than the person's own profile did — and the two numbers being
        # asked to agree is the whole reason someone opens this pane.
        .where(QuizSession.chat_id == chat_id, QuizSession.mode == MODE_EXAM,
               QuizSession.passed.is_not(None)))
    reports = await session.scalar(
        select(func.count(Report.id)).where(Report.chat_id == chat_id))

    return {
        "chat_id": user.chat_id,
        "name": user.display_name,
        "lang": user.lang,
        "created_at": user.created_at,
        "source": user.source,
        "last_seen": last_seen,
        "pass_expires_at": user.pass_expires_at,
        "premium": ent.premium,
        # WHY they have it. Three routes, and "they say the app locked them out" has a
        # different answer for each — a lapsed pass, a channel they left, or staff.
        "premium_via": ("staff" if ent.is_staff else "pass" if ent.has_pass
                        else "channel" if ent.via_channel else "none"),
        "paid_cents": sum(p["amount_cents"] for p in payments if not p["refunded_at"]),
        "payments": payments,
        "answers": answers or 0,
        "exams": exams or 0,
        "reports": reports or 0,
    }


# --- money -------------------------------------------------------------------

@router.get("/payments")
async def payments(
    limit: int = Query(default=20, ge=1, le=100),
    _staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """What has been paid, and by whom. Individual payments were visible nowhere.

    All-time totals existed only by leaving the Mini App and typing /admin to the bot, and
    a single payment could not be seen at all — so "who paid me this month" and "did I ever
    record that 10.99" were both unanswerable from the panel actually being used.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def total(*where):
        return select(func.coalesce(func.sum(Purchase.amount_cents), 0)).where(
            Purchase.amount_cents > 0, Purchase.refunded_at.is_(None), *where)

    rows = list(await session.scalars(
        select(Purchase).where(Purchase.amount_cents > 0)
        .order_by(Purchase.created_at.desc()).limit(limit)))
    names = {
        u.chat_id: u.display_name
        for u in await session.scalars(
            select(User).where(User.chat_id.in_([p.chat_id for p in rows] or [0])))
    }

    return {
        "this_month_cents": await session.scalar(
            total(Purchase.created_at >= month_start)) or 0,
        "all_time_cents": await session.scalar(total()) or 0,
        "payments": [
            {
                "chat_id": p.chat_id,
                "name": names.get(p.chat_id),
                "amount_cents": p.amount_cents,
                "currency": p.currency,
                "tier": p.tier,
                "created_at": p.created_at,
                "refunded_at": p.refunded_at,
            }
            for p in rows
        ],
    }


# --- writing content on purpose ----------------------------------------------

# How many clusters one tap may write. Each is a paid model call taking 10-30 seconds, so
# this is bounded by patience as much as by money: twenty is roughly ten minutes of
# background work and about the most that can finish before anyone wonders if it worked.
MAX_BATCH = 20


@router.post("/content/generate")
async def generate_content(
    count: int = Query(default=10, ge=1, le=MAX_BATCH),
    background: BackgroundTasks = None,
    staff: User = Depends(staff_user),
    session: AsyncSession = Depends(get_session),
):
    """Write explanations for the clusters that cover the MOST questions.

    Until now the bank filled up by accident: `warm` fires when a question happens to be
    served, so what got written was whatever six people wandered into. The only deliberate
    alternative was a CLI script over SSH on a laptop the owner does not use.

    Ordered by cluster SIZE, biggest first, because clusters are wildly uneven — the
    largest covers 34 questions and the median covers two. Writing the big ones first buys
    several times the coverage per euro, and coverage is what a learner actually meets.

    Runs detached and answers at once. Twenty clusters is ten minutes of model calls; there
    is nothing to watch and the result is visible in the coverage numbers afterwards.
    """
    done = select(Explanation.cluster_id).where(
        Explanation.status.in_(SERVABLE_STATUSES))
    targets = list(await session.execute(
        select(Question.cluster_id, func.count(Question.id).label("n"))
        .where(Question.cluster_id.is_not(None), Question.cluster_id.not_in(done))
        .group_by(Question.cluster_id)
        .order_by(func.count(Question.id).desc())
        .limit(count)
    ))
    if not targets:
        raise HTTPException(409, "every cluster already has an explanation")

    covers = sum(n for _cid, n in targets)
    # The cluster IDS, not just how many. Progress is then a question the DATABASE can
    # answer — "how many of these now have an explanation" — instead of a counter held in
    # memory that a deploy silently resets. Two deploys during one batch is exactly how the
    # bar vanished while the work carried on.
    await events.record(session, EV_MODEL_CALL,
                        chat_id=staff.chat_id, kind="batch_requested",
                        cluster_ids=[cid for cid, _n in targets],
                        clusters=len(targets), covers_questions=covers,
                        tokens_in=0, tokens_out=0)
    await session.commit()

    # Bounded, like the prefetch. Unbounded, twenty generations were released at once
    # against a 30,000 TPM account: they do not go faster, they queue behind each other and
    # the whole batch stretches from minutes into the better part of an hour — which is why
    # the number looked frozen.
    gate = asyncio.Semaphore(PREFETCH_CONCURRENCY)

    async def bounded(cluster_id: int):
        async with gate:
            await explanations.warm(cluster_id, staff.lang)

    for cluster_id, _n in targets:
        _detach_admin(bounded(cluster_id))

    return {"started": len(targets), "covers_questions": covers}


# Same reason as the prefetch route's: asyncio keeps only weak references to running tasks,
# so without a strong one the garbage collector may destroy a generation mid-flight.
_batch: set[asyncio.Task] = set()

# How long a batch may look "running" before the bar gives up on it.
#
# Tasks die with the process, so a batch interrupted by a deploy has no one left to finish
# it — and a bar that reports "running" for ever is worse than one that stops, because it
# is a claim rather than an absence. Twenty clusters, three at a time, at 10-30s each is
# under five minutes; fifteen is generous.
BATCH_GIVES_UP_AFTER = timedelta(minutes=15)


def _detach_admin(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _batch.add(task)

    task.add_done_callback(_batch.discard)
    return task


@router.get("/content/progress")
async def content_progress(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    """How far the last batch has got, ASKED OF THE DATABASE.

    This used to be a counter in module state, and it had the one failure a progress bar
    must not have: a deploy reset it while the work carried on, so the bar disappeared and
    the number froze at whatever had last been fetched. Two deploys during one batch is
    exactly what happened.

    Derived instead. The batch records the cluster ids it asked for; progress is how many of
    those now hold a servable explanation. It survives a restart, it cannot drift from the
    coverage number on the same card, and it is right even if the request that started the
    batch came from another device.

    `running` is bounded by TIME as well as by completion. Tasks die with the process, so a
    batch interrupted by a deploy would otherwise report "running" for ever — the bar has to
    be able to give up.
    """
    batches = [
        e for e in (await session.scalars(
            select(Event).where(Event.type == EV_MODEL_CALL)
            .order_by(Event.created_at.desc()).limit(200)))
        if (e.payload or {}).get("kind") == "batch_requested"
    ]
    if not batches:
        return {"total": 0, "done": 0, "running": False}

    batch = batches[0]
    ids = (batch.payload or {}).get("cluster_ids") or []
    if not ids:
        return {"total": (batch.payload or {}).get("clusters", 0), "done": 0,
                "running": False}

    done = await session.scalar(
        select(func.count(func.distinct(Explanation.cluster_id))).where(
            Explanation.cluster_id.in_(ids),
            Explanation.status.in_(SERVABLE_STATUSES)))
    done = done or 0

    age = datetime.now(timezone.utc) - batch.created_at.replace(tzinfo=timezone.utc)
    return {
        "total": len(ids),
        "done": done,
        # Finished, or old enough that whatever was still running is gone.
        "running": done < len(ids) and age < BATCH_GIVES_UP_AFTER,
        "started_at": batch.created_at,
    }


@router.get("/spend")
async def spend(
    _staff: User = Depends(staff_user), session: AsyncSession = Depends(get_session)
):
    """What the model has cost, this month and in total.

    Every call computed its token counts and every caller threw them away, so the running
    cost of the product was unknowable from inside it — the only figure was whatever the
    OpenAI dashboard said days later, with nothing to attribute it to.

    Tokens, not euros. Prices change and are per-model; a number this panel computed from a
    hardcoded rate would be quietly wrong the first time the model does. The dashboard
    converts; this says how much work was done and on what.
    """
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    rows = list(await session.scalars(
        select(Event).where(Event.type == EV_MODEL_CALL)))

    def tally(since=None):
        calls = tokens_in = tokens_out = 0
        for e in rows:
            if since and e.created_at < since:
                continue
            payload = e.payload or {}
            if payload.get("kind") == "batch_requested":
                continue                      # a request, not a call
            calls += 1
            tokens_in += payload.get("tokens_in") or 0
            tokens_out += payload.get("tokens_out") or 0
        return {"calls": calls, "tokens_in": tokens_in, "tokens_out": tokens_out}

    return {"this_month": tally(month_start), "all_time": tally()}
