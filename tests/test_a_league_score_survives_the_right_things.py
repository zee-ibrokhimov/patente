"""Erasing an account takes the season with it. Resetting progress does not.

Two buttons in the product destroy things, and they destroy different things. Getting the
difference wrong is invisible until somebody notices their points came back from the dead.

WHY ERASURE IS THE DANGEROUS ONE

`delete_user` anonymises events rather than deleting them — it nulls the chat id and leaves
the rows. So every ledger DERIVED from the event log resets on erasure, which is the intended
reading of erasure. A ledger kept in its own table keyed on chat_id does not reset, and
`chat_id` is the permanent Telegram id: the same person taps /start, is recreated under the
same key, and finds a season in which every question is already spent and a daily cap already
charged. Verified in the existing data: deleting an account takes its events from five to
zero and leaves its `streak_days` row untouched.

That is why the three league tables carry a real ON DELETE CASCADE and why `delete_user`
names them anyway — the cascade would do it, but the next person to add a table copies what
they can see in that function.

WHY RESET IS THE OPPOSITE

Resetting progress throws away the Leitner schedule. It is "let me start studying again", not
"forget me". Deleting a season's points would make it a way to escape a bad week, and it
would take the streak with it too. Both are deliberately left alone.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from api.models import LeagueDay, LeagueScore, LeagueSlot, Progress, StreakDay
from api.services.telegram_auth import sign
from shared.config import settings

CHAT = 42
TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
WEEK = "2026-08-10"


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = CHAT) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id, "first_name": "Zee"}, separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def seed(api_db, chat_id: int = CHAT) -> None:
    """A learner with a season behind them, in all three tables."""
    async with api_db() as s:
        s.add_all([
            LeagueSlot(chat_id=chat_id, week=WEEK, question_id=1, first_at=NOW,
                       correct=True),
            LeagueSlot(chat_id=chat_id, week=WEEK, question_id=2, first_at=NOW,
                       correct=False),
            LeagueDay(chat_id=chat_id, day="2026-08-12", scored=1, exam_bonus=1),
            LeagueScore(chat_id=chat_id, week=WEEK, points=36, seed=7),
            StreakDay(chat_id=chat_id, day="2026-08-12", qualified_at=NOW, questions=10),
            Progress(chat_id=chat_id, question_id=1),
        ])
        await s.commit()


async def counts(api_db, chat_id: int = CHAT) -> dict[str, int]:
    async with api_db() as s:
        out = {}
        for model in (LeagueSlot, LeagueDay, LeagueScore, StreakDay, Progress):
            out[model.__tablename__] = await s.scalar(
                select(func.count()).select_from(model)
                .where(model.chat_id == chat_id)) or 0
        return out


# --- erasure ------------------------------------------------------------------

async def test_deleting_an_account_removes_every_league_row(client, registered, api_db):
    """Counted in the TABLES, not read off the board.

    A board-level assertion passes with the bug fully intact: the `users` row is gone, the
    board joins against it, so the learner disappears from the screen either way while their
    ledger sits there waiting for them to come back.
    """
    await seed(api_db)
    assert (await counts(api_db))["league_score"] == 1

    assert (await client.delete(f"/users/{CHAT}")).status_code in (200, 204)

    after = await counts(api_db)
    assert after["league_slot"] == 0
    assert after["league_day"] == 0
    assert after["league_score"] == 0
    assert after["streak_days"] == 0, \
        "the streak survived erasure — the same defect this file exists to prevent"


async def test_coming_back_starts_a_clean_season(client, registered, api_db):
    """The point of the rule above, stated as behaviour.

    chat_id is the permanent Telegram id, so somebody who deletes and returns is the same
    key. If the ledger survived, their first answer would be refused as a repeat and their
    daily cap would already be part-spent.
    """
    from api.services import league

    await seed(api_db)
    await client.delete(f"/users/{CHAT}")
    await client.post("/users", json={"chat_id": CHAT, "lang": "ru"})

    async with api_db() as s:
        scored = await league.score_answer(s, CHAT, question_id=1, correct=True, now=NOW)
        await s.commit()
    assert scored is True, "a question spent before erasure was still spent afterwards"
    assert (await counts(api_db))["league_score"] == 1
    async with api_db() as s:
        assert (await s.get(LeagueScore, (CHAT, WEEK))).points == 1, \
            "the old points came back from the dead"


# --- resetting progress -------------------------------------------------------

async def test_resetting_progress_leaves_the_season_alone(client, registered, api_db):
    """Reset is "let me start studying again", not "forget me". Deleting the season would
    make it a way to escape a bad week."""
    await seed(api_db)
    r = await client.post("/webapp/reset", headers=auth())
    assert r.status_code == 200, r.text

    after = await counts(api_db)
    assert after["league_slot"] == 2
    assert after["league_day"] == 1
    assert after["league_score"] == 1
    assert after["streak_days"] == 1


async def test_resetting_progress_still_resets_progress(client, registered, api_db):
    """The guard on the other side. A fix for the test above that simply stopped reset
    deleting anything would pass it."""
    await seed(api_db)
    await client.post("/webapp/reset", headers=auth())
    assert (await counts(api_db))["progress"] == 0


# --- the constraint itself ----------------------------------------------------

async def test_the_tables_carry_a_real_foreign_key(api_db):
    """Asserted against the schema, because the explicit deletes in `delete_user` would
    make the tests above pass without it — and the cascade is what protects any future
    code path that deletes a user without going through that function."""
    from sqlalchemy import text

    async with api_db() as s:
        for table in ("league_slot", "league_day", "league_score"):
            result = await s.execute(text(f"PRAGMA foreign_key_list('{table}')"))
            # By NAME. The columns are (id, seq, table, from, to, on_update, on_delete,
            # match) and the first version of this read on_update — which is "NO ACTION" on
            # a correctly-cascading key, so it failed against working code. Positional
            # indexes into a PRAGMA are a guess dressed as an assertion.
            rows = result.mappings().all()
            assert rows, f"{table} has no foreign key at all"
            assert any(r["table"] == "users" and (r["on_delete"] or "").upper() == "CASCADE"
                       for r in rows), \
                f"{table} does not cascade from users: {[dict(r) for r in rows]}"


async def test_foreign_keys_are_actually_enforced(api_db):
    """A cascade declared on a connection that never enabled foreign keys is decoration.
    SQLite defaults them OFF; this project turns them on per connection."""
    from sqlalchemy import text

    async with api_db() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
