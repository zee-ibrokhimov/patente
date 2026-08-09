"""Give the same days to everyone in a segment.

Asked for as "i need a grants so i can give to my users if they are using my project" —
which the one-at-a-time grant cannot express, because it starts from a search for a name.

Two properties matter more than the feature itself:

  · it EXTENDS each person's own expiry, so somebody mid-way through a paid month is not
    silently cut back to the length of a gift;
  · it cannot be taken back, so it counts first and refuses a confirmation that does not
    match — the same guard the newsletter uses, for a stronger reason. A newsletter that
    goes to the wrong people is embarrassing; access given to the wrong people is
    unrecoverable without taking something away from someone who was told they had it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from api.models import Event, Purchase, User
from tests.test_admin_panel import _staff, auth  # noqa: F401 — autouse staff fixture

OWNER = 42


def when(days_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def a_user(api_db, chat_id: int, *, expires_in: float | None = None,
                 seen_days_ago: float | None = None, paid: bool = False) -> None:
    async with api_db() as s:
        s.add(User(
            chat_id=chat_id, lang="ru",
            pass_expires_at=(None if expires_in is None
                             else datetime.now(timezone.utc) + timedelta(days=expires_in)),
        ))
        if seen_days_ago is not None:
            s.add(Event(chat_id=chat_id, type="answer_given",
                        created_at=when(seen_days_ago)))
        if paid:
            s.add(Purchase(chat_id=chat_id, tribute_purchase_id=f"p{chat_id}",
                           tier="month", amount_cents=999, currency="EUR"))
        await s.commit()


async def count(client, segment: str, within: int = 7) -> int:
    r = await client.post("/webapp/admin/grant-many/preview", headers=auth(),
                          json={"segment": segment, "days": 7, "within_days": within})
    assert r.status_code == 200, r.text
    return r.json()["recipients"]


# --- who is in a segment -----------------------------------------------------

async def test_active_means_used_it_inside_the_window(client, registered, api_db):
    """Measured from the event log — there is no last_seen column, and adding one would be
    a migration to store what is already recorded."""
    await a_user(api_db, 8001, seen_days_ago=2)
    await a_user(api_db, 8002, seen_days_ago=30)

    ids = {8001, 8002}
    within_week = await count(client, "active", within=7)
    within_month = await count(client, "active", within=60)
    assert within_month > within_week, "the window does nothing"

    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "active", "days": 3, "within_days": 7,
        "confirm_recipients": within_week, "notify": False})
    assert r.status_code == 200

    async with api_db() as s:
        recent = await s.get(User, 8001)
        stale = await s.get(User, 8002)
    assert recent.pass_expires_at is not None, "an active user was skipped"
    assert stale.pass_expires_at is None, "somebody outside the window was granted"
    assert ids  # keeps the intent readable


async def test_trial_means_access_with_no_money_behind_it(client, registered, api_db):
    await a_user(api_db, 8010, expires_in=10, paid=False)
    await a_user(api_db, 8011, expires_in=10, paid=True)

    n = await count(client, "trial")
    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "trial", "days": 5, "within_days": 7,
        "confirm_recipients": n, "notify": False})
    assert r.status_code == 200

    async with api_db() as s:
        gifted = await s.get(User, 8010)
        buyer = await s.get(User, 8011)
    assert gifted.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=14)
    assert buyer.pass_expires_at < datetime.now(timezone.utc) + timedelta(days=11), \
        "a paying subscriber was counted as being on a trial"


# --- what it does to an expiry ----------------------------------------------

async def test_it_extends_rather_than_replaces(client, registered, api_db):
    """THE property. Somebody 20 days into a paid month must not be cut back to a 3-day
    gift — that is taking something away while appearing to give."""
    await a_user(api_db, 8020, expires_in=20, seen_days_ago=1)
    n = await count(client, "active")
    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "active", "days": 3, "within_days": 7,
        "confirm_recipients": n, "notify": False})
    assert r.status_code == 200

    async with api_db() as s:
        user = await s.get(User, 8020)
    assert user.pass_expires_at > datetime.now(timezone.utc) + timedelta(days=22), \
        "the existing expiry was replaced by the gift instead of extended"


async def test_a_gift_is_not_recorded_as_revenue(client, registered, api_db):
    """No Purchase row. Counting gifts as sales would make every revenue figure in the
    overview wrong, and would tell the recipient they have a paid subscription."""
    await a_user(api_db, 8030, seen_days_ago=1)
    n = await count(client, "active")
    await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "active", "days": 3, "within_days": 7,
        "confirm_recipients": n, "notify": False})

    async with api_db() as s:
        rows = list(await s.scalars(select(Purchase).where(Purchase.chat_id == 8030)))
    assert rows == [], "a gift was written into the revenue figures"


# --- the guards --------------------------------------------------------------

async def test_it_refuses_a_count_it_was_not_shown(client, registered, api_db):
    await a_user(api_db, 8040, seen_days_ago=1)
    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "active", "days": 3, "within_days": 7,
        "confirm_recipients": 999, "notify": False})
    assert r.status_code == 409
    async with api_db() as s:
        assert (await s.get(User, 8040)).pass_expires_at is None


async def test_it_refuses_without_any_confirmation(client, registered, api_db):
    await a_user(api_db, 8050, seen_days_ago=1)
    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "active", "days": 3, "within_days": 7, "notify": False})
    assert r.status_code == 409


async def test_an_empty_segment_is_refused_rather_than_silently_doing_nothing(
        client, registered, api_db):
    r = await client.post("/webapp/admin/grant-many", headers=auth(), json={
        "segment": "lapsed", "days": 3, "within_days": 7,
        "confirm_recipients": 0, "notify": False})
    assert r.status_code == 409
    assert "nobody" in r.json()["detail"].lower()


async def test_an_unknown_segment_is_refused(client, registered, api_db):
    r = await client.post("/webapp/admin/grant-many/preview", headers=auth(), json={
        "segment": "everyone-please", "days": 3, "within_days": 7})
    assert r.status_code == 422


async def test_the_day_cap_still_applies(client, registered, api_db):
    """A slipped zero on a group grant is the same mistake as on a single one, multiplied
    by the size of the group."""
    r = await client.post("/webapp/admin/grant-many/preview", headers=auth(), json={
        "segment": "active", "days": 3650, "within_days": 7})
    assert r.status_code == 422
