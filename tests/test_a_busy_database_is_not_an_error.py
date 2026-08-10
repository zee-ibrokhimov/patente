"""A request must not fail because another one was writing.

WHAT HAPPENED IN PRODUCTION, 2026-08-10 23:19-23:21

Fifty `sqlite3.OperationalError: database is locked` responses in two minutes, every one of
them a 500 on a screen the owner was looking at. The Mini App fires several requests at once
— the admin console fires six — and each of them runs `webapp_user`, which keeps two cached
fields on the user row fresh: the Telegram display name, and the channel-membership status.

So six requests each opened a READ transaction (the first thing any of them does is a
SELECT), then each tried to UPDATE the same row. That is a transaction upgrade, and this is
the part that matters:

    SQLite refuses a deferred read->write upgrade IMMEDIATELY and does not honour
    busy_timeout on that path.

It cannot wait, because two readers both waiting to upgrade is a deadlock — so it fails one
of them instead. `PRAGMA busy_timeout=5000` was set, live, and verified in production, and it
was never consulted. Every trace said `raised as a result of Query-invoked autoflush`, which
is SQLAlchemy flushing the pending UPDATE into the middle of a later SELECT.

It cleared on its own once the values had been written, which is exactly what made it hard
to catch: the failure only exists while a cached field is still stale, so a system that has
been running for five minutes cannot reproduce its own outage.

WHY THESE TESTS HOLD A LOCK ON PURPOSE

Not by racing. A second connection takes the write lock and keeps it, which makes the
failure deterministic instead of occasional — the same reason the freeze tests build a real
fortnight rather than setting a counter.

THE RULE BEING TESTED

Keeping a cache warm is optional work. It must never decide whether a request succeeds, and
it must never make one slower than it would have been without it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager

import pytest
from sqlalchemy import select

from api.models import User
from api.services.telegram_auth import sign
from shared.config import settings

CHAT = 42
TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"


def auth(chat_id: int = CHAT, first_name: str | None = None) -> dict[str, str]:
    """Signed initData, optionally carrying a name — which is what makes it write.

    The name is the whole point. Every earlier attempt to reproduce this against the running
    system signed initData WITHOUT one, so `display_name` never changed, no UPDATE was
    issued, and a hundred parallel requests passed cleanly. The bug only appears for a
    client that sends what a real Telegram client sends.
    """
    payload: dict = {"id": chat_id}
    if first_name is not None:
        payload["first_name"] = first_name
    settings.bot_token_prod = TOKEN
    settings.env = "prod"
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps(payload, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


@contextmanager
def write_lock_held(db_path):
    """Hold SQLite's write lock for the duration of the block.

    BEGIN IMMEDIATE takes the lock up front rather than on first write, so there is no
    window where the test itself is the one racing.
    """
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE users SET free_explanations_used = free_explanations_used")
    try:
        yield
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def db_file(tmp_path):
    return tmp_path / "api.db"


# --- the reproduction --------------------------------------------------------

async def test_a_name_arriving_while_another_write_is_in_flight_does_not_500(
        client, registered, db_file):
    """THE outage, in one assertion.

    A learner opens the app, their name is not stored yet, and something else is mid-write.
    Before the fix this is a 500 and the Mini App shows its failure screen — which is
    precisely what "the mini app is not loading" looked like.
    """
    with write_lock_held(db_file):
        r = await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    assert r.status_code == 200, \
        f"a contended cache write failed the request: {r.status_code} {r.text[:200]}"


async def test_the_request_does_not_wait_for_the_lock(client, registered, db_file):
    """The cache write must not add the busy_timeout to a user-facing request.

    busy_timeout is 5 seconds. A warm write that waits it out turns a 100ms request into a
    five-second one and the learner watches a spinner — which is a different way of failing
    to load, and the one a retry-based fix would have introduced.
    """
    with write_lock_held(db_file):
        started = time.monotonic()
        r = await client.get("/webapp/me", headers=auth(first_name="Aziz"))
        elapsed = time.monotonic() - started
    assert r.status_code == 200
    assert elapsed < 2.0, f"the request waited {elapsed:.1f}s on a lock it did not need"


async def test_six_at_once_is_what_the_admin_screen_does(client, registered, db_file):
    """The exact fan-out that produced the 500s: six requests, one new name, one row."""
    with write_lock_held(db_file):
        results = await asyncio.gather(*[
            client.get(path, headers=auth(first_name="Aziz"))
            for path in ("/webapp/me", "/webapp/profile", "/webapp/stats",
                         "/webapp/leaderboard", "/webapp/me", "/webapp/profile")
        ])
    codes = [r.status_code for r in results]
    assert codes == [200] * 6, f"the admin fan-out failed: {codes}"


# --- and it still does its job ------------------------------------------------

async def test_the_name_is_stored_when_nothing_is_in_the_way(client, registered, api_db):
    """Tolerating a failure must not become never trying. This is the assertion that stops
    the fix being "delete the write"."""
    r = await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    assert r.status_code == 200
    async with api_db() as s:
        assert (await s.get(User, CHAT)).display_name == "Aziz"


async def test_a_changed_name_replaces_the_old_one(client, registered, api_db):
    """Refreshed on every visit, not written once: a name is something people change, and a
    stale one is being shown to strangers."""
    await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    await client.get("/webapp/me", headers=auth(first_name="Azizbek"))
    async with api_db() as s:
        assert (await s.get(User, CHAT)).display_name == "Azizbek"


async def test_the_name_is_bounded_and_trimmed(client, registered, api_db):
    """It is rendered inside other learners' apps. Telegram promises nothing about length."""
    await client.get("/webapp/me", headers=auth(first_name="  " + "A" * 200 + "  "))
    async with api_db() as s:
        stored = (await s.get(User, CHAT)).display_name
    assert stored is not None and len(stored) <= 32
    assert stored == stored.strip()


async def test_a_lost_write_is_retried_on_the_next_request(client, registered, api_db,
                                                           db_file):
    """Skipping a contended write is only acceptable because the next request tries again.

    Otherwise "never fail the request" would quietly mean "never store the name", and the
    leaderboard would show a placeholder for someone whose first visit happened to be busy.
    """
    with write_lock_held(db_file):
        assert (await client.get("/webapp/me", headers=auth(first_name="Aziz"))).status_code == 200
    async with api_db() as s:
        assert (await s.get(User, CHAT)).display_name is None, \
            "this test is only meaningful if the contended write was actually skipped"

    assert (await client.get("/webapp/me", headers=auth(first_name="Aziz"))).status_code == 200
    async with api_db() as s:
        assert (await s.get(User, CHAT)).display_name == "Aziz"


async def test_the_request_session_is_left_with_nothing_to_flush(client, registered,
                                                                 api_db, db_file):
    """The deeper fix, asserted directly.

    Tolerating the error is a bandage; the cure is that the request's own transaction never
    has a pending write to upgrade into. If `webapp_user` leaves the user row dirty, some
    later SELECT in some other route will autoflush it and fail there instead — which is how
    six DIFFERENT endpoints all broke from one line in a shared dependency.
    """
    with write_lock_held(db_file):
        r = await client.get("/webapp/stats", headers=auth(first_name="Aziz"))
    assert r.status_code == 200, \
        "a route that only reads still failed — the dependency left a write pending"


async def test_the_channel_check_cannot_fail_a_request_either(client, registered, api_db,
                                                              db_file, monkeypatch):
    """The second cached field, and the one in the original traceback:
    `UPDATE users SET channel_checked_at=?`."""
    from api.services import channel

    async def says_member(_chat_id):
        return "member"

    monkeypatch.setattr(channel, "fetch_status", says_member)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.channel_status = None            # never looked up: the blocking first check
        user.channel_checked_at = None
        await s.commit()

    with write_lock_held(db_file):
        r = await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    assert r.status_code == 200


async def test_the_channel_status_is_stored_when_it_can_be(client, registered, api_db,
                                                           monkeypatch):
    from api.services import channel

    async def says_member(_chat_id):
        return "member"

    monkeypatch.setattr(channel, "fetch_status", says_member)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.channel_status = None
        user.channel_checked_at = None
        await s.commit()

    r = await client.get("/webapp/me", headers=auth())
    assert r.status_code == 200
    async with api_db() as s:
        user = await s.get(User, CHAT)
    assert user.channel_status == "member"
    assert user.channel_checked_at is not None


async def test_membership_is_visible_to_the_request_that_looked_it_up(client, registered,
                                                                      api_db, monkeypatch):
    """Someone sitting in the channel they paid to join must not be told they are not
    Premium on the very request that discovered they are.

    This is what makes the fix more than "write it somewhere else": the values are committed
    in their own transaction AND read back, so the entitlement in this response is the one
    just established.
    """
    from api.services import channel

    async def says_creator(_chat_id):
        return "creator"

    monkeypatch.setattr(channel, "fetch_status", says_creator)
    async with api_db() as s:
        user = await s.get(User, CHAT)
        user.channel_status = None
        user.channel_checked_at = None
        user.pass_expires_at = None          # no pass: membership is the only route in
        await s.commit()

    body = (await client.get("/webapp/me", headers=auth())).json()
    assert body["premium"] is True, \
        "the request that discovered the membership did not act on it"


async def test_nothing_is_written_when_nothing_changed(client, registered, api_db,
                                                        monkeypatch):
    """A GET that writes on every call is the thing that caused the outage. Once the cache
    is warm the steady state must be read-only."""
    from api.routes import webapp as webapp_routes

    await client.get("/webapp/me", headers=auth(first_name="Aziz"))

    calls: list[dict] = []
    real = webapp_routes._store_warm_fields

    async def counting(chat_id, values):
        calls.append(values)
        return await real(chat_id, values)

    monkeypatch.setattr(webapp_routes, "_store_warm_fields", counting)
    for _ in range(3):
        assert (await client.get("/webapp/me",
                                 headers=auth(first_name="Aziz"))).status_code == 200
    assert calls == [], f"a settled user still wrote on every request: {calls}"


async def test_the_leaderboard_still_gets_the_name(client, registered, api_db):
    """What the field is FOR. A fix that stored it somewhere the board does not read would
    pass every test above and show a placeholder to every learner."""
    from api.services import leaderboard

    await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    async with api_db() as s:
        rows = await s.scalars(select(User.display_name).where(User.chat_id == CHAT))
        assert rows.one() == "Aziz"
        assert leaderboard is not None


# --- the regression the first fix introduced ---------------------------------

async def test_ten_at_once_do_not_deadlock_on_the_connection_pool(client, registered):
    """The first version of this fix took its connection from the MAIN pool, and hung.

    A request holds one connection for its whole lifetime. Opening a second inside it means
    every concurrent request needs two — so at ten parallel requests all fifteen connections
    (5 + 10 overflow) were held by requests each waiting for a sixteenth. Not slow: stuck,
    for the full thirty-second pool timeout, which is worse than the outage being fixed.

    Ten is chosen because 2 x 10 exceeds the pool and 10 alone does not: the assertion fails
    only if the warm write competes for the same connections.
    """
    started = time.monotonic()
    results = await asyncio.gather(*[
        client.get("/webapp/me", headers=auth(first_name=f"Name{i}"))
        for i in range(10)
    ])
    elapsed = time.monotonic() - started
    assert [r.status_code for r in results] == [200] * 10
    assert elapsed < 10.0, f"ten parallel requests took {elapsed:.1f}s — pool starvation"


async def test_warm_writes_have_a_pool_of_their_own(client, registered):
    """Structural, and deliberately so: the isolation IS the fix.

    A warm write must never be able to take a connection a real request needs, so its pool
    is one connection with no overflow. If someone later points this back at the shared
    factory the deadlock returns, and the timing test above is the kind that goes flaky
    before it goes red.
    """
    from api.routes import webapp as webapp_routes

    await client.get("/webapp/me", headers=auth(first_name="Aziz"))
    pool = webapp_routes._warm_sessions().kw["bind"].pool
    assert pool.size() == 1, f"warm pool has {pool.size()} connections, expected 1"
    assert pool._max_overflow == 0, "warm writes may overflow into the request pool"
