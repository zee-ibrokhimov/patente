"""Access can come back down, and doing so does not message the learner.

Everything else in the panel moves access UP. `grant` is `days >= 1`, `grant-many` only
ever adds, and the only way down was DELETE /users/{chat_id} — erasing somebody's progress,
sessions and answers to correct a date. The group-grant confirm said the quiet part out
loud: "Access cannot be taken back."

THE TRAP, and the reason most of this file exists.

Setting `pass_expires_at = now` drops the user into lapse.py's ended-window
(`pass_expires_at <= now`, within LOOK_BACK = 7 days). The next cron run would then send
them "your Premium has ended", pointing at renewal — for a gift the owner had just quietly
taken back, while probably still in conversation with them. `_already_told` matches on
(chat_id, type, expires_at), so recording the announcement without sending it is what keeps
the bot quiet.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.models import Purchase, User
from api.services import lapse
from tests.test_admin_panel import _staff, auth  # noqa: F401 — autouse staff fixture


def in_days(n: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=n)


async def a_user(api_db, chat_id: int, expires_in: float | None) -> None:
    async with api_db() as s:
        s.add(User(chat_id=chat_id, lang="ru",
                   pass_expires_at=None if expires_in is None else in_days(expires_in)))
        await s.commit()


async def revoke(client, chat_id: int, **body):
    return await client.post(f"/webapp/admin/users/{chat_id}/revoke",
                             headers=auth(), json=body or {"mode": "end"})


# --- what it does ------------------------------------------------------------

async def test_ending_a_pass_ends_it(client, registered, api_db):
    await a_user(api_db, 8100, expires_in=30)
    r = await revoke(client, 8100, mode="end")
    assert r.status_code == 200
    async with api_db() as s:
        user = await s.get(User, 8100)
    assert user.pass_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=5)


async def test_shortening_takes_off_the_days_asked_for(client, registered, api_db):
    await a_user(api_db, 8110, expires_in=30)
    r = await revoke(client, 8110, mode="shorten", days=10)
    assert r.status_code == 200
    async with api_db() as s:
        user = await s.get(User, 8110)
    assert timedelta(days=19) < user.pass_expires_at - datetime.now(timezone.utc) \
        < timedelta(days=21), "shorten did not remove 10 days"


async def test_shortening_past_today_floors_at_now(client, registered, api_db):
    """Never a date in the past. A negative remainder would read as a pass that lapsed
    weeks ago and would be announced as such."""
    await a_user(api_db, 8120, expires_in=3)
    await revoke(client, 8120, mode="shorten", days=99)
    async with api_db() as s:
        user = await s.get(User, 8120)
    assert user.pass_expires_at <= datetime.now(timezone.utc) + timedelta(seconds=5)
    assert user.pass_expires_at > datetime.now(timezone.utc) - timedelta(minutes=1)


async def test_it_never_sets_the_expiry_to_null(client, registered, api_db):
    """NULL and `now` behave the same for the premium count and the trial segment, which
    both filter `> now`. They differ where it matters: lapse.py and the `lapsed` segment
    filter `IS NOT NULL`, so NULL would erase the fact that this person ever had access."""
    await a_user(api_db, 8130, expires_in=30)
    await revoke(client, 8130, mode="end")
    async with api_db() as s:
        assert (await s.get(User, 8130)).pass_expires_at is not None


# --- THE TRAP ----------------------------------------------------------------

async def test_revoking_does_not_trigger_the_lapse_message(client, registered, api_db):
    """The whole point. Without the pre-recorded event, the next cron run tells somebody
    whose gift was quietly withdrawn that their subscription has ended."""
    await a_user(api_db, 8140, expires_in=30)
    await revoke(client, 8140, mode="end")

    sent = []

    async def fake_payment(chat_id, lang, kind, expires_at, tier, days=0):
        sent.append((chat_id, kind))
        return True

    async with api_db() as s:
        from api.services import notify
        original = notify.payment
        notify.payment = fake_payment
        try:
            await lapse.run(s)
        finally:
            notify.payment = original

    assert not any(c == 8140 for c, _ in sent), \
        f"the revoked user was messaged anyway: {sent}"


async def test_a_genuine_lapse_is_still_announced(client, registered, api_db):
    """Guards the guard. Suppressing every notice would be an easy way to pass the test
    above and would silently break the renewal path this product depends on."""
    async with api_db() as s:
        s.add(User(chat_id=8150, lang="ru",
                   pass_expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()

    sent = []

    async def fake_payment(chat_id, lang, kind, expires_at, tier, days=0):
        sent.append((chat_id, kind))
        return True

    async with api_db() as s:
        from api.services import notify
        original = notify.payment
        notify.payment = fake_payment
        try:
            await lapse.run(s)
        finally:
            notify.payment = original

    assert any(c == 8150 for c, _ in sent), \
        "a real lapse stopped being announced — the suppression is too broad"


# --- guards ------------------------------------------------------------------

async def test_it_refuses_when_there_is_nothing_to_take(client, registered, api_db):
    await a_user(api_db, 8160, expires_in=None)
    assert (await revoke(client, 8160, mode="end")).status_code == 409


async def test_an_already_expired_pass_is_refused(client, registered, api_db):
    await a_user(api_db, 8170, expires_in=-5)
    assert (await revoke(client, 8170, mode="end")).status_code == 409


async def test_shorten_needs_a_number(client, registered, api_db):
    await a_user(api_db, 8180, expires_in=30)
    assert (await revoke(client, 8180, mode="shorten", days=0)).status_code == 422


async def test_an_unknown_mode_is_refused(client, registered, api_db):
    await a_user(api_db, 8190, expires_in=30)
    assert (await revoke(client, 8190, mode="obliterate")).status_code == 422


async def test_purchases_are_left_alone(client, registered, api_db):
    """Ending access is not refunding money. `refunded_at` belongs to the refund matcher,
    and the two acts are separate."""
    async with api_db() as s:
        s.add(User(chat_id=8200, lang="ru", pass_expires_at=in_days(30)))
        s.add(Purchase(chat_id=8200, tribute_purchase_id="p8200", tier="month",
                       amount_cents=999, currency="EUR"))
        await s.commit()

    await revoke(client, 8200, mode="end")
    async with api_db() as s:
        row = await s.get(Purchase, 1)
    assert row is not None and row.refunded_at is None, "revoking marked the sale refunded"


async def test_the_previous_expiry_is_recorded(client, registered, api_db):
    """What makes a mistaken revoke undoable: re-granting the difference restores it."""
    await a_user(api_db, 8210, expires_in=30)
    r = await revoke(client, 8210, mode="end")
    assert r.json()["previous"], "the previous expiry was not returned"
