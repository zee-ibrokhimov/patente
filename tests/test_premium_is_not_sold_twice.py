"""Two ways the product asked a paying subscriber to pay again.

Premium has THREE sources — a Tribute pass, membership of the premium channel, or staff.
Two places knew about one of them, and both of those places are about money.

WHY THAT POPULATION IS NOT HYPOTHETICAL

Tribute adds buyers to the channel itself, and on 2026-07-31 its webhooks were refused for
three hours while it was doing exactly that. "Their subscription is real, they are in the
channel, and no pass row exists" is a state this product has already produced once. It also
describes every administrator and the owner.

  1. THE HOURLY JOB TOLD THEM PREMIUM HAD ENDED. `lapse.run` selected on the
     `pass_expires_at` COLUMN and never asked entitlement, so it messaged people whose
     Premium had not ended and whose features all still worked — pointing them at /plan.

  2. /plan THEN SOLD IT TO THEM. `render.plan` read `has_pass` only, so channel and staff
     fell through to the free-tier pitch: "Free plan — translations and explanations are
     Premium", three prices, and a Subscribe button.

AND THE BUTTON DISAGREED WITH THE MESSAGE

`can_subscribe` only ever meant "a checkout link is configured" — a deployment fact with
nothing to do with the user — and the keyboard took it directly. So Subscribe was attached
under EVERY /plan, including the trial message whose whole text is "your subscription will
renew automatically, cancel in @tribute". Message says do not buy, button says buy, and the
button wins because it is the only tappable thing on the screen.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update as sa_update

from api.models import Purchase, User
from api.services import lapse, notify
from bot import keyboards, render
from shared.constants import UI_LANGUAGES

NOW = datetime.now(timezone.utc)
ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    sent = []

    async def fake(chat_id, lang, kind, expires_at, tier, days=0):
        sent.append({"chat_id": chat_id, "kind": kind})
        return True

    monkeypatch.setattr(notify, "payment", fake)
    monkeypatch.setattr(lapse.notify, "payment", fake)
    return sent


@pytest.fixture
def sent(_capture):
    return _capture


async def make(api_db, chat_id, expires, *, channel_status=None, paid=False):
    async with api_db() as s:
        s.add(User(chat_id=chat_id, lang="ru", pass_expires_at=expires,
                   channel_status=channel_status))
        if paid:
            s.add(Purchase(chat_id=chat_id, tribute_purchase_id=f"p{chat_id}",
                           tier="pass_1m", amount_cents=299, currency="eur",
                           extended_to=expires or NOW))
        await s.commit()


# --- 1. the hourly job ------------------------------------------------------

async def test_a_channel_member_is_not_told_their_premium_ended(api_db, sent):
    """Their pass column expired. Their Premium did not — translations, explanations and
    the vocabulary all still work, because `premium` is has_pass OR channel OR staff."""
    await make(api_db, 701, NOW - timedelta(hours=1), channel_status="member")
    async with api_db() as s:
        result = await lapse.run(s)
    assert sent == [], "a subscriber was told their subscription ended"
    assert result["lapsed"] == 0


async def test_the_channel_creator_is_not_told_their_premium_ended(api_db, sent):
    """The owner. Staff are always Premium — this message would be sent to the person who
    runs the product, about a subscription they do not have."""
    await make(api_db, 702, NOW - timedelta(hours=1), channel_status="creator")
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


async def test_a_channel_member_is_not_warned_that_premium_is_ending(api_db, sent):
    """The 3-day warning had the same blind spot: its only guard was a paid Purchase row,
    which channel-granted Premium does not have."""
    await make(api_db, 703, NOW + timedelta(days=2), channel_status="member")
    async with api_db() as s:
        await lapse.run(s)
    assert sent == []


async def test_someone_who_really_lapsed_is_still_told(api_db, sent):
    """The guard must not silence the message this module exists to send."""
    await make(api_db, 704, NOW - timedelta(hours=1), channel_status="left")
    async with api_db() as s:
        await lapse.run(s)
    assert [m["kind"] for m in sent] == ["lapsed"]


async def test_no_event_is_written_for_someone_still_premium(api_db, sent):
    """Recording it would consume the one-shot: if they later leave the channel, that IS a
    real lapse and it has to be announceable."""
    from sqlalchemy import select

    from api.models import Event
    from shared.constants import EV_PASS_LAPSED

    await make(api_db, 705, NOW - timedelta(hours=1), channel_status="member")
    async with api_db() as s:
        await lapse.run(s)

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 705)
                        .values(channel_status="left"))
        await s.commit()
    async with api_db() as s:
        await lapse.run(s)
        rows = (await s.scalars(
            select(Event).where(Event.chat_id == 705, Event.type == EV_PASS_LAPSED))).all()
    assert [m["kind"] for m in sent] == ["lapsed"]
    assert len(rows) == 1


# --- 2. /plan ---------------------------------------------------------------

CHANNEL = {"premium": True, "premium_via": "channel", "has_pass": False,
           "purchased": False, "pass_expires_at": None}
STAFF = {"premium": True, "premium_via": "staff", "has_pass": False,
         "purchased": False, "pass_expires_at": None}
TRIALIST = {"premium": True, "premium_via": "pass", "has_pass": True,
            "purchased": False, "trialing": True,
            "pass_expires_at": (NOW + timedelta(days=5)).isoformat()}
PAID = {"premium": True, "premium_via": "pass", "has_pass": True, "purchased": True,
        "pass_expires_at": (NOW + timedelta(days=60)).isoformat()}
FREE = {"premium": False, "premium_via": "none", "has_pass": False, "purchased": False,
        "pass_expires_at": None}


@pytest.mark.parametrize("lang", UI_LANGUAGES)
@pytest.mark.parametrize("user,label", [(CHANNEL, "channel"), (STAFF, "staff")])
def test_premium_without_a_pass_row_is_not_shown_the_price_list(user, label, lang):
    """`has_pass` is one of three sources. Reading only it sent the other two to the
    free-tier pitch — the surface that owns payment telling a subscriber they have not
    paid."""
    text = render.plan(user, lang, can_subscribe=True)
    prices = ("2,99", "2.99", "7,99", "7.99", "10,99", "10.99")
    assert not any(p in text for p in prices), \
        f"{label}/{lang} was shown the price list: {text[:200]}"


@pytest.mark.parametrize("user,label", [(CHANNEL, "channel"), (STAFF, "staff")])
def test_premium_without_a_pass_row_is_told_premium_is_active(user, label):
    text = render.plan(user, "en", can_subscribe=True)
    assert "Premium is active" in text, f"{label} was not told they have Premium: {text[:200]}"


def test_the_free_pitch_still_reaches_a_free_user():
    """The guard must not switch off the thing /plan is for."""
    text = render.plan(FREE, "en", can_subscribe=True)
    assert any(p in text for p in ("2.99", "2,99")), "a free user was not shown the prices"


# --- 3. the button and the message must agree -------------------------------

@pytest.mark.parametrize("user,label", [
    (TRIALIST, "a trialist whose card WILL be charged"),
    (CHANNEL, "a channel subscriber"),
    (STAFF, "staff"),
    (PAID, "someone who already paid"),
])
def test_no_buy_button_for_anyone_who_already_has_premium(user, label):
    assert render.selling(user, can_subscribe=True) is False, \
        f"a Subscribe button was offered to {label}"


def test_a_free_user_still_gets_the_button():
    assert render.selling(FREE, can_subscribe=True) is True


def test_no_button_when_no_checkout_link_is_configured():
    """The original meaning of can_subscribe, which must survive."""
    assert render.selling(FREE, can_subscribe=False) is False


@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_the_trial_message_and_the_keyboard_do_not_contradict_each_other(lang):
    """THE contradiction. The trial text says the subscription renews automatically and
    tells them where to cancel; the keyboard offered Subscribe directly underneath."""
    sell = render.selling(TRIALIST, can_subscribe=True)
    assert sell is False
    assert keyboards.plan_actions(lang, can_subscribe=sell) is None


# --- 4. the locales -------------------------------------------------------

@pytest.mark.parametrize("lang", UI_LANGUAGES)
def test_both_new_strings_exist_in_every_language(lang):
    data = json.loads((ROOT / f"bot/locales/{lang}.json").read_text(encoding="utf-8"))
    for key in ("plan_active_channel", "plan_active_staff"):
        assert key in data and data[key].strip(), f"{lang} is missing {key}"


def test_the_new_strings_are_not_the_same_in_every_language():
    """The failure mode of every i18n edit in this project: one write landing in all four
    slots, or a locale left holding another language's text."""
    for key in ("plan_active_channel", "plan_active_staff"):
        values = {
            lang: json.loads(
                (ROOT / f"bot/locales/{lang}.json").read_text(encoding="utf-8"))[key]
            for lang in UI_LANGUAGES
        }
        assert len(set(values.values())) == len(values), f"{key} is shared across locales"
