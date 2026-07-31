"""Premium from three places at once: a pass, the channel, or being staff.

Tribute sells a subscription attached to a Telegram channel and adds buyers to it. That
makes membership a second, independent record of who has paid — held on Telegram's
servers rather than in our database.

The owner asked for membership AND a Tribute subscription to be required together. It is
built as OR, and the reason is not theoretical: on 2026-07-31 this app was unreachable for
three hours and every Tribute webhook was answered 530 by Cloudflare. Anyone who paid in
that window would have had no pass here while sitting in the channel Tribute had just
added them to. Under AND they would have been locked out by the same outage that failed
them. Under OR the channel carries them.

Nobody reaches that channel without paying or being added by the owner, so OR gives away
nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.models import User
from api.services import channel
from api.services.entitlement import evaluate
from shared.config import settings

FUTURE = datetime.now(timezone.utc) + timedelta(days=10)
PAST = datetime.now(timezone.utc) - timedelta(days=1)


def user(**kw) -> User:
    base = dict(chat_id=1, lang="ru", pass_expires_at=None, free_explanations_used=0)
    return User(**{**base, **kw})


@pytest.fixture(autouse=True)
def _no_configured_admins(monkeypatch):
    """Otherwise a leaked ADMIN_CHAT_IDS from the environment makes every case pass."""
    monkeypatch.setattr(settings, "admin_chat_ids", "")


# --- the matrix -------------------------------------------------------------

@pytest.mark.parametrize("status, expected", [
    ("creator", True),
    ("administrator", True),
    ("member", True),
    ("restricted", True),     # still in the channel, just limited in what they can post
    ("left", False),
    ("kicked", False),
    (None, False),            # never checked
    ("unknown", False),
])
def test_channel_status_decides_premium(status, expected):
    assert evaluate(user(channel_status=status)).premium is expected


def test_a_pass_alone_is_enough():
    """The customer who paid but whose channel invite never landed must not be locked
    out — the whole reason this is OR and not AND."""
    e = evaluate(user(pass_expires_at=FUTURE, channel_status="left"))
    assert e.premium is True
    assert e.has_pass is True


def test_membership_alone_is_enough():
    """The customer whose webhook we never received. Exactly what the 2026-07-31 outage
    would have produced."""
    e = evaluate(user(channel_status="member"))
    assert e.premium is True
    assert e.has_pass is False
    assert e.via_channel is True


def test_neither_means_no_premium():
    assert evaluate(user(channel_status="left", pass_expires_at=PAST)).premium is False


def test_an_expired_pass_does_not_linger():
    assert evaluate(user(pass_expires_at=PAST)).premium is False


# --- staff ------------------------------------------------------------------

@pytest.mark.parametrize("status", ["creator", "administrator"])
def test_channel_admins_are_premium_without_paying(status):
    """They run the product. Asking them to subscribe to themselves is absurd, and this
    is how the owner exercises paid features without buying them."""
    e = evaluate(user(channel_status=status))
    assert e.is_staff is True
    assert e.premium is True


def test_a_plain_member_is_not_staff():
    assert evaluate(user(channel_status="member")).is_staff is False


def test_a_configured_owner_is_premium_even_outside_the_channel(monkeypatch):
    monkeypatch.setattr(settings, "admin_chat_ids", "357127133")
    e = evaluate(user(chat_id=357127133, channel_status="left"))
    assert e.is_staff is True
    assert e.premium is True


def test_configuring_an_owner_does_not_make_everyone_staff(monkeypatch):
    monkeypatch.setattr(settings, "admin_chat_ids", "357127133")
    assert evaluate(user(chat_id=999)).is_staff is False


# --- every paid feature has to follow, not just some ------------------------

@pytest.mark.parametrize("status", ["member", "administrator"])
def test_channel_premium_unlocks_all_paid_features(status):
    """A second entitlement source that only some features honour is worse than none —
    the learner is Premium on one screen and not on the next."""
    e = evaluate(user(channel_status=status))
    assert e.can_translate
    assert e.can_explain
    assert e.can_use_vocabulary
    assert e.can_use_spaced_repetition


def test_channel_premium_does_not_burn_a_free_taster():
    """spends_free_explanation must be false for anyone Premium, however they got there.
    Otherwise a channel member silently consumes the lifetime tasters they never needed."""
    assert evaluate(user(channel_status="member")).spends_free_explanation is False


# --- the cache ---------------------------------------------------------------

def test_a_never_checked_status_is_stale():
    assert channel.is_stale(None) is True


def test_a_fresh_check_is_not_stale():
    assert channel.is_stale(datetime.now(timezone.utc)) is False


def test_an_old_check_is_stale():
    assert channel.is_stale(datetime.now(timezone.utc) - timedelta(hours=2)) is True


def test_a_naive_timestamp_does_not_explode():
    """SQLite hands back naive datetimes depending on how a row was written; comparing
    one to an aware `now` raises TypeError and would 500 every request."""
    assert channel.is_stale(datetime.now() - timedelta(hours=2)) is True


async def test_a_failed_lookup_never_downgrades_a_member(monkeypatch):
    """The critical one. If a Telegram blip were recorded as "left", every paying
    channel member would lose Premium at once — a self-inflicted mass revocation."""
    u = user(channel_status="member",
             channel_checked_at=datetime.now(timezone.utc) - timedelta(hours=2))

    async def fails(chat_id):
        return None

    monkeypatch.setattr(channel, "fetch_status", fails)
    assert await channel.refresh(None, u) == "member"
    assert u.channel_status == "member"


async def test_a_failed_lookup_does_not_reset_the_timer(monkeypatch):
    """It must retry on the next request rather than trust a value it could not confirm
    for another fifteen minutes."""
    stamp = datetime.now(timezone.utc) - timedelta(hours=2)
    u = user(channel_status="member", channel_checked_at=stamp)

    async def fails(chat_id):
        return None

    monkeypatch.setattr(channel, "fetch_status", fails)
    await channel.refresh(None, u)
    assert u.channel_checked_at == stamp


async def test_a_real_answer_of_left_does_downgrade(monkeypatch):
    """The guard above must not turn into "membership can never be lost"."""
    u = user(channel_status="member",
             channel_checked_at=datetime.now(timezone.utc) - timedelta(hours=2))

    async def gone(chat_id):
        return "left"

    monkeypatch.setattr(channel, "fetch_status", gone)
    assert await channel.refresh(None, u) == "left"
    assert evaluate(u).premium is False


async def test_a_fresh_status_is_not_re_fetched(monkeypatch):
    """Every check is a Telegram API call. Re-asking on each request would rate-limit
    the bot for an answer that changes maybe once a month."""
    calls = []

    async def counting(chat_id):
        calls.append(chat_id)
        return "member"

    monkeypatch.setattr(channel, "fetch_status", counting)
    u = user(channel_status="member", channel_checked_at=datetime.now(timezone.utc))
    await channel.refresh(None, u)
    assert calls == []


async def test_force_overrides_the_ttl(monkeypatch):
    async def answer(chat_id):
        return "administrator"

    monkeypatch.setattr(channel, "fetch_status", answer)
    u = user(channel_status="member", channel_checked_at=datetime.now(timezone.utc))
    assert await channel.refresh(None, u, force=True) == "administrator"


async def test_no_channel_configured_means_no_calls(monkeypatch):
    """The app has to run unchanged for anyone who has not set one up."""
    monkeypatch.setattr(settings, "premium_channel_id", "")
    assert await channel.fetch_status(1) is None
