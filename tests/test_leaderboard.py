"""The weekly league, and the privacy rules that make it publishable.

This is the ONLY response in the product that carries one learner's personal data to
another, so the tests that matter most here are not the ranking ones — they are the ones
about what never leaves.

Three rules, all enforced in the service rather than the route, so a second caller cannot
get them wrong:

  · someone who opted out is ABSENT from the ranking entirely, not merely hidden — they do
    not occupy a place, and their absence leaves no gap to infer them from;
  · nothing but a first name and a score is ever returned. No chat id, no username, nothing
    that could be used to FIND a person rather than rank them;
  · the opt-out is retroactive, because one that only applied from next Monday would not be
    an opt-out.

WEEKLY, AND ON CORRECT ANSWERS
All-time is won permanently by whoever arrived first, so for everyone after them it is a
screen showing they have already lost. Counting attempts rather than correct answers rewards
tapping fast and being wrong, on a product whose whole pitch is understanding the question —
and it is farmable by holding down VERO.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update as sa_update

from api.models import Event, User
from api.services import leaderboard
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import EV_ANSWER_GIVEN, LEADERBOARD_SIZE

TOKEN = "8918020834:AAEtest-token-not-real-only-for-tests"
OWNER = 42
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token_prod", TOKEN)
    monkeypatch.setattr(settings, "env", "prod")


def auth(chat_id: int = OWNER) -> dict:
    return {"X-Telegram-Init-Data": sign(
        {"user": json.dumps({"id": chat_id, "first_name": "Zee"},
                            separators=(",", ":")),
         "auth_date": str(int(time.time()))}, TOKEN)}


async def learner(api_db, chat_id: int, name: str, *, correct: int, wrong: int = 0,
                  when=None, opted_out: bool = False):
    """Someone with a week's activity behind them."""
    when = when or (leaderboard.week_start() + timedelta(hours=2))
    async with api_db() as s:
        if await s.get(User, chat_id) is None:
            s.add(User(chat_id=chat_id, lang="ru", display_name=name,
                       leaderboard_opt_out=opted_out))
        else:
            await s.execute(sa_update(User).where(User.chat_id == chat_id).values(
                display_name=name, leaderboard_opt_out=opted_out))
        for i in range(correct):
            s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                        payload={"correct": True}, created_at=when + timedelta(seconds=i)))
        for i in range(wrong):
            s.add(Event(chat_id=chat_id, type=EV_ANSWER_GIVEN,
                        payload={"correct": False},
                        created_at=when + timedelta(seconds=100 + i)))
        await s.commit()


async def board(api_db, chat_id: int = OWNER):
    async with api_db() as s:
        return await leaderboard.board(s, await s.get(User, chat_id))


# --- the ranking -------------------------------------------------------------

async def test_the_board_ranks_by_correct_answers(api_db, registered):
    await learner(api_db, OWNER, "Zee", correct=5)
    await learner(api_db, 101, "Aziz", correct=9)
    await learner(api_db, 102, "Dilnoza", correct=7)

    entries = (await board(api_db))["entries"]
    assert [e["name"] for e in entries] == ["Aziz", "Dilnoza", "Zee"]
    assert [e["rank"] for e in entries] == [1, 2, 3]


async def test_wrong_answers_do_not_score(api_db, registered):
    """Otherwise the board ranks whoever taps fastest without reading — farmable by holding
    down VERO, on a product sold on understanding the question."""
    await learner(api_db, OWNER, "Zee", correct=3, wrong=50)
    await learner(api_db, 101, "Aziz", correct=4)
    entries = (await board(api_db))["entries"]
    assert entries[0]["name"] == "Aziz"


async def test_last_weeks_work_does_not_count(api_db, registered):
    """A fixed week is what makes the board winnable by someone who joins late."""
    last_week = leaderboard.week_start() - timedelta(days=3)
    await learner(api_db, 101, "Aziz", correct=99, when=last_week)
    await learner(api_db, OWNER, "Zee", correct=1)
    entries = (await board(api_db))["entries"]
    assert [e["name"] for e in entries] == ["Zee"]


async def test_somebody_who_has_not_answered_is_not_listed(api_db, registered):
    """A board padded with zeroes is a list of people who are not playing."""
    await learner(api_db, OWNER, "Zee", correct=2)
    await learner(api_db, 101, "Aziz", correct=0)
    assert [e["name"] for e in (await board(api_db))["entries"]] == ["Zee"]


async def test_the_board_is_capped(api_db, registered):
    await learner(api_db, OWNER, "Zee", correct=1)
    for i in range(LEADERBOARD_SIZE + 6):
        await learner(api_db, 200 + i, f"Learner {i}", correct=i + 2)
    result = await board(api_db)
    assert len(result["entries"]) == LEADERBOARD_SIZE
    assert result["ranked"] == LEADERBOARD_SIZE + 7


async def test_ties_are_ordered_stably(api_db, registered):
    """An unstable order makes the board look like it is flickering when nothing changed."""
    await learner(api_db, OWNER, "Zee", correct=4)
    await learner(api_db, 101, "Aziz", correct=4)
    await learner(api_db, 102, "Dilnoza", correct=4)
    first = [e["name"] for e in (await board(api_db))["entries"]]
    second = [e["name"] for e in (await board(api_db))["entries"]]
    assert first == second


# --- where the caller stands -------------------------------------------------

async def test_the_caller_can_find_themselves(api_db, registered):
    await learner(api_db, OWNER, "Zee", correct=3)
    await learner(api_db, 101, "Aziz", correct=9)
    result = await board(api_db)
    assert result["me"]["rank"] == 2
    assert result["me"]["score"] == 3
    assert [e["is_me"] for e in result["entries"]] == [False, True]


async def test_the_caller_is_told_their_rank_even_outside_the_top(api_db, registered):
    """"You are 22nd with 12" is information. A board they cannot find themselves on is
    just other people."""
    await learner(api_db, OWNER, "Zee", correct=1)
    for i in range(LEADERBOARD_SIZE + 4):
        await learner(api_db, 300 + i, f"L{i}", correct=i + 5)
    result = await board(api_db)
    assert result["me"]["rank"] == LEADERBOARD_SIZE + 5
    assert all(not e["is_me"] for e in result["entries"])


async def test_someone_who_has_not_played_has_no_rank(api_db, registered):
    await learner(api_db, 101, "Aziz", correct=4)
    result = await board(api_db)
    assert result["me"]["rank"] is None
    assert result["me"]["score"] == 0


# --- privacy -----------------------------------------------------------------

async def test_opting_out_removes_them_from_the_ranking(api_db, registered):
    """ABSENT, not hidden. Leaving a gap where they were would let anyone infer both that
    they exist and roughly what they scored."""
    await learner(api_db, OWNER, "Zee", correct=2)
    await learner(api_db, 101, "Aziz", correct=9, opted_out=True)
    await learner(api_db, 102, "Dilnoza", correct=5)

    result = await board(api_db)
    assert [e["name"] for e in result["entries"]] == ["Dilnoza", "Zee"]
    assert [e["rank"] for e in result["entries"]] == [1, 2], \
        "an opted-out learner still occupied a place"
    assert result["ranked"] == 2


async def test_opting_out_is_retroactive(api_db, registered):
    """One that only applied from next Monday would not be an opt-out."""
    await learner(api_db, 101, "Aziz", correct=9)
    assert "Aziz" in [e["name"] for e in (await board(api_db))["entries"]]

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 101).values(
            leaderboard_opt_out=True))
        await s.commit()
    assert "Aziz" not in [e["name"] for e in (await board(api_db))["entries"]]


async def test_an_opted_out_caller_is_told_so(api_db, registered):
    """Rather than being shown an empty board and left to wonder if it is broken."""
    await learner(api_db, OWNER, "Zee", correct=3, opted_out=True)
    assert (await board(api_db))["me"]["opted_out"] is True


async def test_nothing_identifying_ever_leaves(api_db, registered, client):
    """THE test. A first name and a score cannot be used to find someone; a chat id or a
    username can. Asserted on the serialised response, not the dict, because the schema is
    what actually reaches another person's phone."""
    await learner(api_db, OWNER, "Zee", correct=2)
    await learner(api_db, 101, "Aziz", correct=9)

    r = await client.get("/webapp/leaderboard", headers=auth())
    assert r.status_code == 200, r.text
    blob = json.dumps(r.json())
    assert "101" not in blob, "a chat id reached another learner"
    for forbidden in ("chat_id", "username", "lang", "pass_expires", "channel"):
        assert forbidden not in blob, f"{forbidden} reached another learner"


async def test_a_learner_with_no_name_is_still_ranked(api_db, registered):
    """Skipping them would make every rank below them wrong."""
    await learner(api_db, OWNER, "Zee", correct=2)
    async with api_db() as s:
        s.add(User(chat_id=101, lang="ru", display_name=None))
        for i in range(9):
            s.add(Event(chat_id=101, type=EV_ANSWER_GIVEN, payload={"correct": True},
                        created_at=leaderboard.week_start() + timedelta(hours=3, seconds=i)))
        await s.commit()

    entries = (await board(api_db))["entries"]
    assert [e["rank"] for e in entries] == [1, 2]
    assert entries[0]["name"] is None


# --- the small-N problem, reported rather than hidden ------------------------

async def test_the_board_reports_how_many_are_playing(api_db, registered):
    """With four users somebody is permanently last, which is demoralising rather than
    motivating. The client needs to know it is too quiet to render as a competition."""
    await learner(api_db, OWNER, "Zee", correct=2)
    await learner(api_db, 101, "Aziz", correct=9)
    assert (await board(api_db))["ranked"] == 2


# --- through the API ---------------------------------------------------------

async def test_the_endpoint_requires_a_signature(client, registered):
    assert (await client.get("/webapp/leaderboard")).status_code == 401


async def test_the_endpoint_returns_the_week_it_covers(client, registered, api_db):
    await learner(api_db, OWNER, "Zee", correct=1)
    body = (await client.get("/webapp/leaderboard", headers=auth())).json()
    assert body["week_start"].startswith(leaderboard.week_start().date().isoformat())


def test_the_week_starts_on_monday():
    from datetime import date

    for day in range(1, 8):
        start = leaderboard.week_start(date(2026, 7, day))
        assert start.weekday() == 0, f"{date(2026, 7, day)} mapped to {start}"


def test_the_week_is_utc_for_everyone():
    """A per-learner week cannot be ranked: two people comparing positions would be
    comparing different windows."""
    assert leaderboard.week_start().tzinfo == timezone.utc
