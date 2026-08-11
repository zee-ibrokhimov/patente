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

from api.models import LeagueScore, User
from api.services import leaderboard
from api.services.telegram_auth import sign
from shared.config import settings
from shared.constants import (
    LEADERBOARD_SIZE,
    LEAGUE_MEDAL_PLACES,
    LEAGUE_MIN_POINTS,
    LEAGUE_PRIZE_MIN_RANKED,
)

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


async def learner(api_db, chat_id: int, name: str | None, *, points: int,
                  week=None, opted_out: bool = False, seed: int | None = None):
    """Someone with a season's points behind them.

    Writes the running total DIRECTLY, and that is deliberate. This file tests the BOARD —
    ordering, the ranking floor, the opt-out, what may leave the server — and driving every
    one of those through the scoring rules would mean forty credited answers to forty
    distinct questions per learner per test, which tests `league.score_answer` for the
    twentieth time and the board once.

    How a point is EARNED is tested in tests/test_the_weekly_league.py, through
    `record_answer`, where it belongs.

    The previous version of this helper wrote raw answer events with no `question_id` and no
    `credited`, all inside one day, and handed out scores of 1 to 23. Every one of those is
    now structurally wrong: below the twenty-point floor nobody holds a rank at all.
    """
    week = week or leaderboard.week_start().date().isoformat()
    async with api_db() as s:
        if await s.get(User, chat_id) is None:
            s.add(User(chat_id=chat_id, lang="ru", display_name=name,
                       leaderboard_opt_out=opted_out))
        else:
            await s.execute(sa_update(User).where(User.chat_id == chat_id).values(
                display_name=name, leaderboard_opt_out=opted_out))
        # Before the score row: league_score carries a real FOREIGN KEY to users now, and
        # foreign_keys=ON is set per connection, so an unflushed user is a constraint error
        # rather than an orphan row. That constraint is the point — it is what makes
        # `/delete` take the season with it.
        await s.flush()
        # Upserted, not inserted: a test that raises a learner's score has to be able to
        # call this twice for the same person and season.
        existing = await s.get(LeagueScore, (chat_id, week))
        if existing is not None:
            existing.points = points
            if seed is not None:
                existing.seed = seed
        elif points:
            # `seed` is the tiebreak. Defaulted to the INVERSE of chat_id so that any test
            # asserting a tie order is asserting the seed did the work — if the service
            # silently fell back to ordering by chat_id, the expected order would flip.
            s.add(LeagueScore(chat_id=chat_id, week=week, points=points,
                              seed=seed if seed is not None else 1_000_000 - chat_id))
        await s.commit()


async def board(api_db, chat_id: int = OWNER):
    async with api_db() as s:
        return await leaderboard.board(s, await s.get(User, chat_id))


# --- the ranking -------------------------------------------------------------

async def test_the_board_ranks_by_correct_answers(api_db, registered):
    await learner(api_db, OWNER, "Zee", points=25)
    await learner(api_db, 101, "Aziz", points=29)
    await learner(api_db, 102, "Dilnoza", points=27)

    entries = (await board(api_db))["entries"]
    assert [e["name"] for e in entries] == ["Aziz", "Dilnoza", "Zee"]
    assert [e["rank"] for e in entries] == [1, 2, 3]


async def test_more_points_ranks_higher(api_db, registered):
    """The old name was `test_wrong_answers_do_not_score`, and it belonged to a board that
    counted raw events. Whether a wrong answer scores is now decided in `league.score_answer`
    and is tested there, against the real write path — see
    tests/test_the_weekly_league.py. What is left here is the ordering itself."""
    await learner(api_db, OWNER, "Zee", points=23)
    await learner(api_db, 101, "Aziz", points=24)
    entries = (await board(api_db))["entries"]
    assert entries[0]["name"] == "Aziz"


async def test_last_weeks_work_does_not_count(api_db, registered):
    """A fixed week is what makes the board winnable by someone who joins late."""
    last_week = (leaderboard.week_start().date() - timedelta(days=7)).isoformat()
    await learner(api_db, 101, "Aziz", points=119, week=last_week)
    await learner(api_db, OWNER, "Zee", points=21)
    entries = (await board(api_db))["entries"]
    assert [e["name"] for e in entries] == ["Zee"]


async def test_somebody_who_has_not_answered_is_not_listed(api_db, registered):
    """A board padded with zeroes is a list of people who are not playing."""
    await learner(api_db, OWNER, "Zee", points=22)
    await learner(api_db, 101, "Aziz", points=0)
    assert [e["name"] for e in (await board(api_db))["entries"]] == ["Zee"]


async def test_the_board_is_capped(api_db, registered):
    await learner(api_db, OWNER, "Zee", points=21)
    for i in range(LEADERBOARD_SIZE + 6):
        await learner(api_db, 200 + i, f"Learner {i}", points=LEAGUE_MIN_POINTS + i + 2)
    result = await board(api_db)
    assert len(result["entries"]) == LEADERBOARD_SIZE
    assert result["ranked"] == LEADERBOARD_SIZE + 7


async def test_ties_are_ordered_stably(api_db, registered):
    """An unstable order makes the board look like it is flickering when nothing changed."""
    await learner(api_db, OWNER, "Zee", points=24)
    await learner(api_db, 101, "Aziz", points=24)
    await learner(api_db, 102, "Dilnoza", points=24)
    first = [e["name"] for e in (await board(api_db))["entries"]]
    second = [e["name"] for e in (await board(api_db))["entries"]]
    assert first == second


# --- where the caller stands -------------------------------------------------

async def test_the_caller_can_find_themselves(api_db, registered):
    await learner(api_db, OWNER, "Zee", points=23)
    await learner(api_db, 101, "Aziz", points=29)
    result = await board(api_db)
    assert result["me"]["rank"] == 2
    assert result["me"]["score"] == 23
    assert [e["is_me"] for e in result["entries"]] == [False, True]


async def test_the_caller_is_told_their_rank_even_outside_the_top(api_db, registered):
    """"You are 22nd with 12" is information. A board they cannot find themselves on is
    just other people."""
    await learner(api_db, OWNER, "Zee", points=21)
    for i in range(LEADERBOARD_SIZE + 4):
        await learner(api_db, 300 + i, f"L{i}", points=LEAGUE_MIN_POINTS + i + 5)
    result = await board(api_db)
    assert result["me"]["rank"] == LEADERBOARD_SIZE + 5
    assert all(not e["is_me"] for e in result["entries"])


async def test_someone_who_has_not_played_has_no_rank(api_db, registered):
    await learner(api_db, 101, "Aziz", points=24)
    result = await board(api_db)
    assert result["me"]["rank"] is None
    assert result["me"]["score"] == 0


# --- privacy -----------------------------------------------------------------

async def test_opting_out_removes_them_from_the_ranking(api_db, registered):
    """ABSENT, not hidden. Leaving a gap where they were would let anyone infer both that
    they exist and roughly what they scored."""
    await learner(api_db, OWNER, "Zee", points=22)
    await learner(api_db, 101, "Aziz", points=29, opted_out=True)
    await learner(api_db, 102, "Dilnoza", points=25)

    result = await board(api_db)
    assert [e["name"] for e in result["entries"]] == ["Dilnoza", "Zee"]
    assert [e["rank"] for e in result["entries"]] == [1, 2], \
        "an opted-out learner still occupied a place"
    assert result["ranked"] == 2


async def test_opting_out_is_retroactive(api_db, registered):
    """One that only applied from next Monday would not be an opt-out."""
    await learner(api_db, 101, "Aziz", points=29)
    assert "Aziz" in [e["name"] for e in (await board(api_db))["entries"]]

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 101).values(
            leaderboard_opt_out=True))
        await s.commit()
    assert "Aziz" not in [e["name"] for e in (await board(api_db))["entries"]]


async def test_an_opted_out_caller_is_told_so(api_db, registered):
    """Rather than being shown an empty board and left to wonder if it is broken."""
    await learner(api_db, OWNER, "Zee", points=23, opted_out=True)
    assert (await board(api_db))["me"]["opted_out"] is True


async def test_nothing_identifying_ever_leaves(api_db, registered, client):
    """THE test. A first name and a score cannot be used to find someone; a chat id or a
    username can. Asserted on the serialised response, not the dict, because the schema is
    what actually reaches another person's phone."""
    await learner(api_db, OWNER, "Zee", points=22)
    await learner(api_db, 101, "Aziz", points=29)

    r = await client.get("/webapp/leaderboard", headers=auth())
    assert r.status_code == 200, r.text
    blob = json.dumps(r.json())
    assert "101" not in blob, "a chat id reached another learner"
    for forbidden in ("chat_id", "username", "lang", "pass_expires", "channel"):
        assert forbidden not in blob, f"{forbidden} reached another learner"


async def test_a_learner_with_no_name_is_still_ranked(api_db, registered):
    """Skipping them would make every rank below them wrong."""
    await learner(api_db, OWNER, "Zee", points=22)
    await learner(api_db, 101, None, points=29)

    entries = (await board(api_db))["entries"]
    assert [e["rank"] for e in entries] == [1, 2]
    assert entries[0]["name"] is None


# --- the small-N problem, reported rather than hidden ------------------------

async def test_the_board_reports_how_many_are_playing(api_db, registered):
    """With four users somebody is permanently last, which is demoralising rather than
    motivating. The client needs to know it is too quiet to render as a competition."""
    await learner(api_db, OWNER, "Zee", points=22)
    await learner(api_db, 101, "Aziz", points=29)
    assert (await board(api_db))["ranked"] == 2


# --- through the API ---------------------------------------------------------

async def test_the_endpoint_requires_a_signature(client, registered):
    assert (await client.get("/webapp/leaderboard")).status_code == 401


async def test_the_endpoint_returns_the_week_it_covers(client, registered, api_db):
    await learner(api_db, OWNER, "Zee", points=21)
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


# --- the ranking floor -------------------------------------------------------

async def test_below_the_floor_nobody_holds_a_rank(api_db, registered):
    """One correct answer used to occupy a place on the board. That is absurd once a place
    carries a medal and, later, a prize."""
    await learner(api_db, OWNER, "Zee", points=LEAGUE_MIN_POINTS)
    await learner(api_db, 101, "Aziz", points=LEAGUE_MIN_POINTS - 1)

    result = await board(api_db)
    assert [e["name"] for e in result["entries"]] == ["Zee"]
    assert result["ranked"] == 1, "somebody under the floor was counted as ranked"


async def test_the_floor_is_exactly_the_floor(api_db, registered):
    """Asserted on both sides of the boundary in one test, because a strict/loose comparison
    error moves it by exactly one and every other test in this file passes either way."""
    await learner(api_db, 101, "Aziz", points=LEAGUE_MIN_POINTS - 1)
    assert (await board(api_db))["ranked"] == 0
    await learner(api_db, 101, "Aziz", points=LEAGUE_MIN_POINTS)
    assert (await board(api_db))["ranked"] == 1


async def test_a_caller_below_the_floor_sees_their_points_but_no_rank(api_db, registered):
    """Telling somebody who answered fifteen questions that their score is zero — which the
    old board did — is simply wrong. They are unranked, not scoreless."""
    await learner(api_db, OWNER, "Zee", points=LEAGUE_MIN_POINTS - 5)
    result = await board(api_db)
    assert result["me"]["score"] == LEAGUE_MIN_POINTS - 5
    assert result["me"]["rank"] is None


# --- the tiebreak ------------------------------------------------------------

async def test_ties_are_not_broken_by_account_age(api_db, registered):
    """Under a daily ceiling exact ties are the NORMAL case, so this decides who takes the
    medal every week.

    Ordering by chat_id hands every tie to the oldest Telegram account permanently, and
    publishes a total ordering of the ranked population by signup date to anyone who can read
    a score. The seeds here are set so that ordering by chat_id gives the opposite answer.
    """
    await learner(api_db, 101, "Older", points=40, seed=900)
    await learner(api_db, 202, "Newer", points=40, seed=100)
    names = [e["name"] for e in (await board(api_db))["entries"]]
    assert names == ["Newer", "Older"], \
        f"ties broken by account age rather than by seed: {names}"


async def test_a_tie_keeps_its_order_across_calls(api_db, registered):
    """An unstable order makes the board look like it is flickering when nothing changed."""
    await learner(api_db, 101, "Aziz", points=40, seed=5)
    await learner(api_db, 202, "Dilnoza", points=40, seed=9)
    first = [e["name"] for e in (await board(api_db))["entries"]]
    second = [e["name"] for e in (await board(api_db))["entries"]]
    assert first == second == ["Aziz", "Dilnoza"]


# --- medals ------------------------------------------------------------------

async def _finished_season(api_db, n: int, week: str, base: int = 500):
    """A previous season with `n` ranked learners, best first by construction."""
    for i in range(n):
        await learner(api_db, base + i, f"Past {i}", points=100 - i, week=week,
                      seed=i)


def _last_week() -> str:
    return (leaderboard.week_start().date() - timedelta(days=7)).isoformat()


async def test_last_seasons_top_three_carry_a_medal(api_db, registered):
    await _finished_season(api_db, LEAGUE_PRIZE_MIN_RANKED, _last_week())
    for i in range(LEAGUE_PRIZE_MIN_RANKED):
        await learner(api_db, 500 + i, f"Past {i}", points=30 + i)

    entries = (await board(api_db))["entries"]
    medals = {e["name"]: e["medal"] for e in entries if e["medal"]}
    assert medals == {"Past 0": 1, "Past 1": 2, "Past 2": 3}, medals
    assert sum(1 for e in entries if e["medal"]) == LEAGUE_MEDAL_PLACES


async def test_a_medal_is_from_the_immediately_preceding_season_only(api_db, registered):
    """A stack of six medals beside one name rebuilds the all-time board — a screen that
    tells every newcomer they have already lost."""
    two_weeks_ago = (leaderboard.week_start().date() - timedelta(days=14)).isoformat()
    await _finished_season(api_db, LEAGUE_PRIZE_MIN_RANKED, two_weeks_ago)
    for i in range(LEAGUE_PRIZE_MIN_RANKED):
        await learner(api_db, 500 + i, f"Past {i}", points=30 + i)

    entries = (await board(api_db))["entries"]
    assert all(e["medal"] is None for e in entries), \
        "a medal from two seasons ago is still being worn"


async def test_a_season_too_quiet_to_award_hands_out_no_medals(api_db, registered):
    """Three of ten is a podium; three of five is a participation trophy."""
    await _finished_season(api_db, LEAGUE_PRIZE_MIN_RANKED - 1, _last_week())
    for i in range(LEAGUE_PRIZE_MIN_RANKED - 1):
        await learner(api_db, 500 + i, f"Past {i}", points=30 + i)

    entries = (await board(api_db))["entries"]
    assert all(e["medal"] is None for e in entries)


async def test_prize_eligibility_is_its_own_threshold(api_db, registered):
    """Separate from the client's "too quiet to draw as a competition" line, and asserted
    while the board is still fully populated — the two numbers are different and a test that
    only checked an empty board would not know which one it was proving."""
    for i in range(LEAGUE_PRIZE_MIN_RANKED - 1):
        await learner(api_db, 600 + i, f"L{i}", points=30 + i)
    result = await board(api_db)
    assert len(result["entries"]) == LEAGUE_PRIZE_MIN_RANKED - 1
    assert result["prize_eligible"] is False

    await learner(api_db, 700, "One more", points=25)
    assert (await board(api_db))["prize_eligible"] is True


async def test_an_opted_out_podium_finisher_takes_their_medal_with_them(api_db, registered):
    """The accepted cost of computing medals live rather than storing them.

    A stored medal would have to hold a name or a chat id, and it would then survive both
    erasure and the opt-out — for exactly the three most visible learners on the board. The
    retroactive opt-out is the promise this product actually made, so it wins.
    """
    await _finished_season(api_db, LEAGUE_PRIZE_MIN_RANKED, _last_week())
    for i in range(LEAGUE_PRIZE_MIN_RANKED):
        await learner(api_db, 500 + i, f"Past {i}", points=30 + i)
    assert any(e["medal"] == 1 for e in (await board(api_db))["entries"])

    async with api_db() as s:
        await s.execute(sa_update(User).where(User.chat_id == 500).values(
            leaderboard_opt_out=True))
        await s.commit()
    entries = (await board(api_db))["entries"]
    assert all(e["name"] != "Past 0" for e in entries)
    assert [e["medal"] for e in entries if e["medal"]] == [], \
        "the season fell below the award threshold, so nobody should wear a medal"
