"""Only a referral link grants a trial. A bare /start grants nothing.

Payment moved off Tribute on 2026-08-09 to a direct arrangement — someone messages the owner
and is granted access by hand. Tribute owned the trial, so removing it left the trial with no
delivery mechanism at all.

Handing one to everybody who taps /start would give the product away to anyone who finds the
bot in search. So the trial rides on the LINK: `t.me/<bot>?start=<code>` grants whatever that
code is worth, and a bare /start grants nothing. A code posted to a chosen channel is a trial
the owner decided to give a chosen audience; a stranger arriving unprompted is not.

ONE FACT, NOT TWO. `users.source` has recorded the /start payload since before this existed,
for acquisition reporting. The trial reads the SAME field, so "where did they come from" and
"what were they given" cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from api.models import Event, ReferralLink, User
from api.services import referrals, users
from api.services.referrals import MAX_TRIAL_DAYS
from shared.constants import EV_TRIAL_STARTED


async def link(api_db, code: str, *, days: int = 7, active: bool = True,
               max_uses: int | None = None):
    async with api_db() as s:
        s.add(ReferralLink(code=code, label=code, trial_days=days,
                           active=active, max_uses=max_uses))
        await s.commit()


async def arrive(api_db, chat_id: int, source: str | None = None) -> User:
    async with api_db() as s:
        user, _created = await users.get_or_create(s, chat_id, "ru", source=source)
        await s.commit()
    async with api_db() as s:
        return await s.get(User, chat_id)


def has_trial(user: User) -> bool:
    return bool(user.pass_expires_at
                and user.pass_expires_at > datetime.now(timezone.utc))


# --- the rule ----------------------------------------------------------------

async def test_a_bare_start_grants_nothing(api_db):
    """THE change. Anyone who finds the bot in search gets the free tier, not the product."""
    user = await arrive(api_db, 9001)
    assert not has_trial(user), "a stranger was handed a trial"


async def test_a_referral_link_grants_its_days(api_db):
    await link(api_db, "tg_uzbeks", days=14)
    user = await arrive(api_db, 9002, "tg_uzbeks")
    assert has_trial(user)
    days = (user.pass_expires_at - datetime.now(timezone.utc)).days
    assert 13 <= days <= 14


async def test_each_link_carries_its_own_length(api_db):
    """An influencer's audience can be worth fourteen days where a cold channel is worth
    three. That is a judgement about the audience, not about the product."""
    await link(api_db, "warm", days=14)
    await link(api_db, "cold", days=3)
    warm = await arrive(api_db, 9003, "warm")
    cold = await arrive(api_db, 9004, "cold")
    assert (warm.pass_expires_at - cold.pass_expires_at).days >= 10


async def test_an_unknown_code_grants_nothing(api_db):
    """Someone guessing at a link, or a code that was deleted."""
    user = await arrive(api_db, 9005, "made_up")
    assert not has_trial(user)
    assert user.source == "made_up", "attribution is still recorded, only the trial is not"


async def test_a_switched_off_link_grants_nothing(api_db):
    await link(api_db, "expired_campaign", active=False)
    user = await arrive(api_db, 9006, "expired_campaign")
    assert not has_trial(user)


async def test_switching_off_does_not_erase_who_it_brought(api_db):
    """Off rather than deleted, always. Deleting the row would erase the attribution of
    every user the link ever brought."""
    await link(api_db, "campaign", days=7)
    await arrive(api_db, 9007, "campaign")
    async with api_db() as s:
        row = await s.get(ReferralLink, "campaign")
        row.active = False
        await s.commit()
    later = await arrive(api_db, 9008, "campaign")
    assert not has_trial(later)
    async with api_db() as s:
        first = await s.get(User, 9007)
    assert first.source == "campaign"
    assert has_trial(first), "an existing trial was revoked by switching the link off"


# --- caps --------------------------------------------------------------------

async def test_a_capped_link_stops_granting(api_db):
    """What makes a link safe to post somewhere public."""
    await link(api_db, "limited", days=7, max_uses=2)
    assert has_trial(await arrive(api_db, 9010, "limited"))
    assert has_trial(await arrive(api_db, 9011, "limited"))
    assert not has_trial(await arrive(api_db, 9012, "limited")), "the cap did not hold"


async def test_uses_are_counted_from_the_users_table(api_db):
    """Counted from `users.source` rather than a counter on the link, so the cap cannot
    disagree with the acquisition report and cannot be corrupted by a failed transaction."""
    await link(api_db, "counted", days=7)
    await arrive(api_db, 9013, "counted")
    await arrive(api_db, 9014, "counted")
    async with api_db() as s:
        assert await referrals.uses(s, "counted") == 2


async def test_an_uncapped_link_keeps_going(api_db):
    await link(api_db, "open", days=7, max_uses=None)
    for chat_id in range(9020, 9025):
        assert has_trial(await arrive(api_db, chat_id, "open"))


# --- it cannot be claimed twice ----------------------------------------------

async def test_returning_through_the_same_link_grants_nothing_more(api_db):
    """`get_or_create` returns early for anyone who exists, so re-clicking does nothing.
    Without that, a link would be an unlimited supply of Premium to one person."""
    await link(api_db, "again", days=7)
    first = await arrive(api_db, 9030, "again")
    expiry = first.pass_expires_at
    second = await arrive(api_db, 9030, "again")
    assert second.pass_expires_at == expiry


async def test_an_existing_user_cannot_claim_a_link_later(api_db):
    """Someone already registered who is then sent a referral link. The trial is for first
    contact; letting it apply afterwards makes every link a coupon for existing users."""
    await arrive(api_db, 9031)
    await link(api_db, "late", days=14)
    user = await arrive(api_db, 9031, "late")
    assert not has_trial(user)


async def test_the_first_link_keeps_the_attribution(api_db):
    """Unchanged, and deliberate: someone who arrives again through a different link was
    still acquired by the first one."""
    await link(api_db, "first", days=7)
    await link(api_db, "second", days=7)
    await arrive(api_db, 9032, "first")
    user = await arrive(api_db, 9032, "second")
    assert user.source == "first"


# --- the bound ---------------------------------------------------------------

def test_a_link_cannot_hand_out_a_year():
    """A cap on the blast radius of a typo. Three months of Premium given away by a slipped
    zero is not recoverable once somebody has it."""
    row = ReferralLink(code="x", trial_days=3650)
    assert referrals.trial_days_for(row) == MAX_TRIAL_DAYS


def test_a_negative_length_grants_nothing():
    assert referrals.trial_days_for(ReferralLink(code="x", trial_days=-5)) == 0


def test_no_link_is_no_days():
    assert referrals.trial_days_for(None) == 0


# --- the record --------------------------------------------------------------

async def test_the_trial_event_names_the_link(api_db):
    """The conversion question worth answering is not "do trials work" but "which audience
    converts", and that is only separable if the code is on the event from the first one.
    Events cannot be backfilled."""
    await link(api_db, "tracked", days=7)
    await arrive(api_db, 9040, "tracked")
    async with api_db() as s:
        row = (await s.scalars(select(Event).where(
            Event.chat_id == 9040, Event.type == EV_TRIAL_STARTED))).one()
    assert row.payload["code"] == "tracked"
    assert row.payload["days"] == 7


async def test_no_trial_means_no_trial_event(api_db):
    """Otherwise the funnel counts trials nobody was given."""
    await arrive(api_db, 9041)
    async with api_db() as s:
        rows = (await s.scalars(select(Event).where(
            Event.chat_id == 9041, Event.type == EV_TRIAL_STARTED))).all()
    assert rows == []
