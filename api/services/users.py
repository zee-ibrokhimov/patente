"""User lifecycle, including the GDPR erasure path.

What is stored is deliberately minimal: a Telegram chat id and an answer history.
No names, no usernames, no message text (plan §5). That is what makes /delete a
short, provable operation rather than a hunt.

Erasure policy, per table:

  users, progress, reports   deleted outright — this is the personal data
  events                     chat_id set to NULL, rows kept. The person becomes
                             unidentifiable while the aggregate metrics that
                             every §9 number derives from stay intact. Deleting
                             them would silently rewrite historical conversion.
  purchases                  kept. Retained under the accounting obligation, and
                             needed to honour a later refund webhook, which
                             matches on the Tribute purchase id rather than on
                             the person.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, Progress, Report, User
from api.services import events, referrals
from shared.constants import (
    DEFAULT_LANG,
    EV_SESSION_START,
    EV_TRIAL_STARTED,
    EV_USER_DELETED,
    UI_LANGUAGES,
)


def _clean_source(value: str | None) -> str | None:
    """A deep-link payload is user-controlled text arriving from a URL.

    Telegram already limits it to 64 characters of [A-Za-z0-9_-], but this is written to
    the database and read back onto an admin screen, so it is not taken on trust.
    """
    if not value:
        return None
    cleaned = "".join(c for c in value.strip() if c.isalnum() or c in "_-")[:64]
    return cleaned or None


async def get_or_create(
    session: AsyncSession, chat_id: int, lang: str | None = None,
    source: str | None = None,
) -> tuple[User, bool]:
    user = await session.get(User, chat_id)
    if user is not None:
        # Never overwritten. Someone who arrives again through a different link was still
        # acquired by the first one, and letting the newest claim them would credit
        # whichever channel they happened to revisit rather than the one that worked.
        # Backfilled only if it was missing entirely — which is the case for everyone who
        # signed up before this existed.
        if source and not user.source:
            user.source = _clean_source(source)
        return user, False

    # The trial, granted at first contact, to first contact only, and ONLY through a
    # referral link.
    #
    # Payment moved off Tribute on 2026-08-09 to a direct arrangement, which left the trial
    # with no delivery mechanism. Handing one to everybody who taps /start would give the
    # product away to anyone who finds the bot in search, so a bare /start now grants
    # nothing and `t.me/<bot>?start=<code>` grants whatever that code is worth. See
    # api/services/referrals.py.
    #
    # Deliberately here rather than in a route: /start and opening the Mini App both land in
    # this one function, and a trial granted in one but not the other would be a trial some
    # users never got.
    #
    # It is NOT a Purchase row. Purchases are money — they drive revenue reporting and are
    # what a refund is matched against (plan §4.1) — so inventing one for a trial would
    # corrupt both. `pass_expires_at` is set directly and a distinct event records it,
    # which also keeps trials out of the conversion funnel: someone who converts AFTER a
    # trial is the number that matters, and it is only separable if the two are distinct
    # events from the start. Events cannot be backfilled.
    now = datetime.now(timezone.utc)
    code = _clean_source(source)
    link = await referrals.redeemable(session, code)
    days = referrals.trial_days_for(link)

    user = User(
        source=code,
        chat_id=chat_id,
        lang=lang if lang in UI_LANGUAGES else DEFAULT_LANG,
        pass_expires_at=now + timedelta(days=days) if days else None,
    )
    session.add(user)
    await session.flush()
    await events.record(session, EV_SESSION_START, chat_id=chat_id, first_seen=True)
    if days:
        await events.record(
            session, EV_TRIAL_STARTED, chat_id=chat_id,
            days=days, expires_at=user.pass_expires_at.isoformat(),
            # Which link paid for it. The conversion question worth answering is not "do
            # trials work" but "which audience converts", and that is only separable if the
            # code is on the event from the first one.
            code=code,
        )
    return user, True


async def update_settings(
    session: AsyncSession,
    user: User,
    lang: str | None = None,
    translations_on: bool | None = None,
    onboarded: bool | None = None,
    leaderboard_opt_out: bool | None = None,
) -> User:
    if lang is not None:
        if lang not in UI_LANGUAGES:
            raise ValueError(f"unsupported language {lang!r}")
        user.lang = lang
    if translations_on is not None:
        user.translations_on = translations_on
    if onboarded and user.onboarded_at is None:
        user.onboarded_at = datetime.now(timezone.utc)
    if leaderboard_opt_out is not None:
        # Retroactive by construction: the ranking query filters on this column every time
        # it runs, so opting out removes them from the CURRENT week as well as future ones.
        # An opt-out that only applied from next Monday would not be one.
        user.leaderboard_opt_out = leaderboard_opt_out
    await session.flush()
    return user


async def delete_user(session: AsyncSession, chat_id: int) -> bool:
    user = await session.get(User, chat_id)
    if user is None:
        return False

    await events.record(session, EV_USER_DELETED, chat_id=chat_id)
    await session.flush()

    # Anonymise before deleting, so the erasure event itself carries no id either.
    await session.execute(
        update(Event).where(Event.chat_id == chat_id).values(chat_id=None)
    )
    await session.execute(delete(Report).where(Report.chat_id == chat_id))
    await session.execute(delete(Progress).where(Progress.chat_id == chat_id))
    await session.delete(user)
    await session.flush()
    return True


async def count_progress(session: AsyncSession, chat_id: int) -> int:
    return len((await session.scalars(
        select(Progress.question_id).where(Progress.chat_id == chat_id)
    )).all())
