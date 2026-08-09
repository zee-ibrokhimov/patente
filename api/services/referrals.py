"""Referral links, and the trial that only they can give.

Payment moved off Tribute on 2026-08-09 to a direct arrangement: someone messages the owner
and is granted access by hand. Tribute owned the trial, so removing it left the trial with
no delivery mechanism at all — and the obvious replacement, handing one to everybody who
taps /start, gives the product away to anyone who finds the bot in search.

So the trial rides on the LINK. `t.me/quizpatente_bot?start=<code>` grants whatever that
code is worth; a bare /start grants nothing. That is a deliberate asymmetry: a code posted
to a chosen channel is a trial the owner decided to give to a chosen audience, and a
stranger arriving unprompted is not.

ONE FACT, NOT TWO

`users.source` has recorded the /start payload since before any of this existed, for
acquisition reporting. The trial now reads the SAME field, so "where did they come from" and
"what were they given" cannot drift apart, and there is no second column to keep in step.

WHAT A LINK CANNOT DO

  · grant twice — the trial is written at first contact and `get_or_create` returns early
    for anyone who already exists, so re-clicking a link does nothing;
  · outlive being switched off — `active` is checked at redemption, not at creation;
  · exceed its cap — `max_uses` is counted from `users.source`, which is the same number
    the admin screen shows, so the cap cannot disagree with the report.

Switched off rather than deleted, always. Deleting a link would erase the attribution of
every user it ever brought.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import ReferralLink, User

log = logging.getLogger(__name__)

# Nobody gets more than this from a link, whatever the row says. A cap on the blast radius
# of a typo in the admin screen: three months of Premium given away by a slipped zero is not
# recoverable once someone has it.
MAX_TRIAL_DAYS = 60


async def uses(session: AsyncSession, code: str) -> int:
    """How many users arrived through this code.

    Counted from `users.source` rather than a counter on the link, so it cannot drift from
    the acquisition report and cannot be corrupted by a failed transaction.
    """
    return int(await session.scalar(
        select(func.count(User.chat_id)).where(User.source == code)
    ) or 0)


async def redeemable(session: AsyncSession, code: str | None) -> ReferralLink | None:
    """The link this code can actually claim right now, or None.

    Every reason to refuse is checked here rather than at the call site, so a second caller
    cannot forget one.
    """
    if not code:
        return None
    link = await session.get(ReferralLink, code)
    if link is None:
        log.info("unknown referral code %r — no trial", code)
        return None
    if not link.active:
        log.info("referral code %r is switched off — no trial", code)
        return None
    if link.max_uses is not None and await uses(session, code) >= link.max_uses:
        log.info("referral code %r has reached %s uses — no trial", code, link.max_uses)
        return None
    return link


def trial_days_for(link: ReferralLink | None) -> int:
    """How many days this link is worth, bounded.

    Bounded here rather than only at creation, so a row edited directly in the database —
    or created before the bound existed — still cannot hand out a year.
    """
    if link is None:
        return 0
    return max(0, min(MAX_TRIAL_DAYS, link.trial_days))
