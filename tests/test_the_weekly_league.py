"""Earning a point: one per question per week, and every way that can be gamed.

The board is tested in tests/test_leaderboard.py. This file is only about the write side —
whether something that just happened is worth a point — and it drives the REAL write path
(`record_answer`, and the exam grader) rather than calling the scorer directly. That
distinction is the whole value of the file: a test that seeds a `league_day` row and then
calls `score_answer` never exercises the `WHERE scored < cap` clause that actually enforces
the cap, and would pass with the enforcement deleted.

THE RULE THAT DOES THE WORK is one point per QUESTION per week, spent on the first answer.
Three ordinary features would otherwise be scoring engines: a repeat round is an unlimited
stream of questions the learner already knows the answer to, an exam re-serves questions they
have seen, and practice hands a missed question back after ten minutes by design.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from api.models import LeagueDay, LeagueScore, LeagueSlot, Question, QuizSession, User
from api.services import league, pacing
from api.services.answers import record_answer
from api.services.entitlement import evaluate
from shared.constants import (
    LEAGUE_DAILY_ANSWER_CAP,
    LEAGUE_EXAM_BONUS,
    MODE_EXAM,
    MODE_PRACTICE,
)

CHAT = 42
# Wednesday, so a week boundary is three days away in each direction.
NOW = datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc)
WEEK = "2026-08-10"


@pytest.fixture(autouse=True)
def _always_credited(monkeypatch):
    """Pacing is tested in its own file; here it would only add sleeps.

    Overridden explicitly in the one test that cares, so "uncredited answers do not score"
    is asserted rather than assumed.
    """
    async def credited(_session, _chat_id, _now=None):
        return True

    monkeypatch.setattr(pacing, "check", credited)


async def answer(api_db, question_id: int, correct: bool = True, when=None,
                 chat_id: int = CHAT) -> dict:
    """One answer through the real write path."""
    when = when or NOW
    async with api_db() as s:
        user = await s.get(User, chat_id)
        question = await s.get(Question, question_id)
        # The fixture bank holds four questions; the tests that need more invent ids, which
        # `record_answer` never dereferences beyond `question.id` and `question.answer`.
        result = await record_answer(s, user, question, question.answer is correct,
                                     evaluate(user), now=when)
        await s.commit()
    return result


async def points(api_db, chat_id: int = CHAT, week: str = WEEK) -> int:
    async with api_db() as s:
        row = await s.get(LeagueScore, (chat_id, week))
    return row.points if row else 0


async def slots(api_db, chat_id: int = CHAT) -> int:
    async with api_db() as s:
        return await s.scalar(
            select(func.count()).select_from(LeagueSlot)
            .where(LeagueSlot.chat_id == chat_id)) or 0


# --- one point per question per week -----------------------------------------

async def test_a_correct_answer_scores_a_point(api_db, registered):
    assert (await answer(api_db, 1))["league_point"] is True
    assert await points(api_db) == 1


async def test_the_same_question_twice_in_a_week_scores_once(api_db, registered):
    """Asserted on the DELTA, not on `points == 1`. A scorer that silently failed to write
    the second time also leaves the total at 1, so the weaker assertion passes for the
    wrong reason."""
    await answer(api_db, 1)
    before = await points(api_db)
    second = await answer(api_db, 1)
    assert second["league_point"] is False
    assert await points(api_db) - before == 0


async def test_a_wrong_first_answer_spends_the_question_for_the_week(api_db, registered):
    """Deliberate: refunding the slot would make guess-then-retry the optimal play, which is
    exactly the behaviour a product built on understanding the question exists to discourage.

    Both halves are asserted. A test that only checked the final score would also pass if the
    wrong answer had simply been ignored — which is a different rule with the same total.
    """
    first = await answer(api_db, 1, correct=False)
    assert first["league_point"] is False
    assert await slots(api_db) == 1, "the wrong answer did not spend the slot"

    later = await answer(api_db, 1, correct=True, when=NOW + timedelta(hours=2))
    assert later["league_point"] is False
    assert await points(api_db) == 0


async def test_the_same_question_scores_again_next_week(api_db, registered):
    await answer(api_db, 1)
    await answer(api_db, 1, when=NOW + timedelta(days=7))
    assert await points(api_db) == 1
    assert await points(api_db, week="2026-08-17") == 1


async def test_an_uncredited_answer_scores_nothing_and_spends_nothing(api_db, registered,
                                                                      monkeypatch):
    """Both halves matter, and the second is the interesting one.

    If a too-fast answer SPENT the question, a sub-second blitz would let somebody burn a
    week of their own questions permanently and never earn them back by studying properly —
    and one real sitting in the production log answered 23 questions in 85 seconds.
    """
    async def refused(_session, _chat_id, _now=None):
        return False

    monkeypatch.setattr(pacing, "check", refused)
    assert (await answer(api_db, 1))["league_point"] is False
    assert await points(api_db) == 0
    assert await slots(api_db) == 0, "an uncredited answer burned the question for the week"


async def test_one_learners_slot_is_not_anothers(api_db, registered, client):
    await client.post("/users", json={"chat_id": 777, "lang": "ru"})
    await answer(api_db, 1, chat_id=CHAT)
    await answer(api_db, 1, chat_id=777)
    assert await points(api_db, CHAT) == 1
    assert await points(api_db, 777) == 1


# --- the daily ceiling --------------------------------------------------------

async def test_the_cap_is_enforced_by_the_database(api_db, registered, monkeypatch):
    """Driven through `record_answer` one answer at a time, past the cap.

    A test that pre-seeded a `league_day` row and called `score_answer` would never execute
    the `WHERE scored < cap` clause, and would pass with the enforcement deleted.
    """
    monkeypatch.setattr(league, "LEAGUE_DAILY_ANSWER_CAP", 2)
    # Four DISTINCT questions against a cap of two. Distinct matters: a repeat would be
    # refused by the slot rule instead, and the test would pass without the cap existing.
    for q in (1, 2, 3, 4):
        await answer(api_db, q, when=NOW + timedelta(minutes=q))
    async with api_db() as s:
        day = await s.get(LeagueDay, (CHAT, "2026-08-12"))
    assert day.scored == 2, f"the cap did not bind: scored={day.scored}"
    assert await points(api_db) == 2
    assert await slots(api_db) == 4, "all four questions should still have been claimed"


async def test_the_cap_resets_the_next_day(api_db, registered, monkeypatch):
    monkeypatch.setattr(league, "LEAGUE_DAILY_ANSWER_CAP", 2)
    for q in (1, 2, 3):
        await answer(api_db, q, when=NOW)
    await answer(api_db, 4, when=NOW + timedelta(days=1))
    assert await points(api_db) == 3, "the cap did not reset at midnight UTC"


async def test_an_answer_past_the_cap_still_spends_its_slot(api_db, registered,
                                                            monkeypatch):
    """Accepted, not accidental. The slot must be claimed before the cap is consulted, or
    the cap could be dodged by answering in the right order; releasing it afterwards means a
    DELETE on the hot path and reopens "answer everything cheaply now, re-answer for points
    later"."""
    monkeypatch.setattr(league, "LEAGUE_DAILY_ANSWER_CAP", 1)
    await answer(api_db, 1)
    await answer(api_db, 2)
    assert await points(api_db) == 1
    assert await slots(api_db) == 2


# --- the mock-exam bonus ------------------------------------------------------

async def _exam(api_db, passed: bool, chat_id: int = CHAT, finished=None,
                mode: str = MODE_EXAM):
    """A graded sitting, straight into the grader."""
    from api.services import quiz_sessions

    finished = finished or NOW
    async with api_db() as s:
        row = QuizSession(chat_id=chat_id, mode=mode, question_count=30,
                          max_errors=3, started_at=NOW - timedelta(minutes=20),
                          expires_at=NOW + timedelta(minutes=10))
        s.add(row)
        await s.flush()
        row.answered = 30
        row.wrong = 0 if passed else 10
        await quiz_sessions._grade(s, row, state="submitted", finished_at=finished,
                                   now=NOW)
        await s.commit()
    return row


async def test_passing_a_mock_exam_pays_the_bonus(api_db, registered):
    await _exam(api_db, passed=True)
    assert await points(api_db) == LEAGUE_EXAM_BONUS


async def test_a_second_pass_the_same_day_pays_nothing(api_db, registered):
    """Asserted on the ledger as well as the total. "The score did not change" is also true
    if the second sitting simply failed to grade."""
    await _exam(api_db, passed=True)
    await _exam(api_db, passed=True, finished=NOW + timedelta(hours=3))
    assert await points(api_db) == LEAGUE_EXAM_BONUS
    async with api_db() as s:
        assert (await s.get(LeagueDay, (CHAT, "2026-08-12"))).exam_bonus == 1


async def test_a_failed_exam_pays_nothing(api_db, registered):
    await _exam(api_db, passed=False)
    assert await points(api_db) == 0


async def test_a_finished_practice_sitting_pays_nothing(api_db, registered):
    """`_grade` is otherwise mode-blind. Without the `mode == MODE_EXAM` guard every End
    test pays the bonus."""
    await _exam(api_db, passed=True, mode=MODE_PRACTICE)
    assert await points(api_db) == 0


async def test_a_pass_discovered_after_the_season_closed_pays_nothing(api_db, registered):
    """An expired sitting is graded whenever somebody next looks at it — gaps of an hour
    exist in the production log. Paying it into the current week would put points in a season
    the work did not happen in, and a closed season has already shown its medals."""
    from api.services import quiz_sessions

    sunday = datetime(2026, 8, 9, 23, 30, tzinfo=timezone.utc)   # last week
    async with api_db() as s:
        row = QuizSession(chat_id=CHAT, mode=MODE_EXAM, question_count=30, max_errors=3,
                          started_at=sunday - timedelta(minutes=20), expires_at=sunday)
        s.add(row)
        await s.flush()
        row.answered = 30
        row.wrong = 0
        await quiz_sessions._grade(s, row, state="expired", finished_at=sunday, now=NOW)
        await s.commit()
    assert await points(api_db, week=WEEK) == 0
    assert await points(api_db, week="2026-08-03") == 0


async def test_the_bonus_is_not_a_multiple_of_the_daily_cap(api_db, registered):
    """A privacy rule wearing a constant's clothes.

    With a bonus equal to the cap, `score = answers + bonus x exams` is uniquely solvable
    from one screenshot — on a Monday any score between 41 and 80 is exactly one pass — so
    the board would publish how many mock exams a NAMED learner has passed, to people who may
    be in their driving school.
    """
    assert LEAGUE_EXAM_BONUS % LEAGUE_DAILY_ANSWER_CAP != 0
    assert LEAGUE_DAILY_ANSWER_CAP % LEAGUE_EXAM_BONUS != 0


# --- the week key -------------------------------------------------------------

def test_the_week_key_is_the_monday_not_an_iso_week_number():
    """`strftime('%Y-%W')` splits Monday 2025-12-29 into '2025-52' and '2026-00'; the ISO
    (year, week) pair collides with it, because that Monday is ISO 2026-W01 inside calendar
    year 2025. A Monday date has neither problem."""
    monday = datetime(2025, 12, 29, 12, tzinfo=timezone.utc)
    sunday = datetime(2026, 1, 4, 23, 59, tzinfo=timezone.utc)
    assert league.week_of(monday) == "2025-12-29"
    assert league.week_of(sunday) == "2025-12-29"
    assert league.week_of(monday + timedelta(days=7)) == "2026-01-05"


def test_the_week_key_matches_what_the_board_reports():
    """Two ways of naming the same week is how a season silently splits in half."""
    from api.services import leaderboard

    for offset in range(0, 21, 3):
        moment = datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(days=offset)
        assert league.week_of(moment) == leaderboard.week_start(moment.date()).date().isoformat()


def test_the_day_is_utc_even_though_the_streak_day_is_not():
    """Both exist and they differ. A UTC week contains eight distinct Rome dates, so a Rome
    day here would let the boundary Monday's cap straddle two seasons."""
    from api.services import streak

    late = datetime(2026, 8, 12, 23, 30, tzinfo=timezone.utc)   # 01:30 on the 13th in Rome
    assert league.utc_day(late) == "2026-08-12"
    assert streak.rome_day(late).isoformat() == "2026-08-13"


async def test_the_tiebreak_seed_is_not_the_account_id(api_db, registered):
    """Written by the SERVICE, so it has to be asserted against the service.

    The board tests write score rows directly with chosen seeds, which proves the board
    orders by whatever is in the column — and would keep passing if the scorer wrote the
    chat id into it. Then every tie would go to the oldest Telegram account forever, and the
    board would publish its population's signup order to anyone who can read a score.
    """
    await answer(api_db, 1)
    async with api_db() as s:
        row = await s.get(LeagueScore, (CHAT, WEEK))
    assert row.seed != CHAT, "the tiebreak is the account id"
    assert row.seed > 0


async def test_the_seed_does_not_move_once_the_season_has_started(api_db, registered):
    """Otherwise the board reshuffles under a tie every time anybody scores, which reads as
    flickering — the exact thing a stable tiebreak exists to prevent."""
    await answer(api_db, 1)
    async with api_db() as s:
        first = (await s.get(LeagueScore, (CHAT, WEEK))).seed
    await answer(api_db, 2)
    async with api_db() as s:
        assert (await s.get(LeagueScore, (CHAT, WEEK))).seed == first


# --- retention ----------------------------------------------------------------

async def test_old_ledgers_are_pruned_but_the_totals_are_kept(api_db, registered):
    """`league_slot` is one row per question per learner per week — about a gigabyte a year
    at ten thousand learners, on a host with six free, and nothing else in this repo prunes
    anything but Docker images and backup files.

    The totals stay. They are the history, they are small, and the board reads them.
    """
    from api.models import LeagueDay as Day
    from api.models import LeagueScore as Score
    from api.models import LeagueSlot as Slot

    old_week, old_day = "2026-06-01", "2026-06-03"
    async with api_db() as s:
        s.add_all([
            Slot(chat_id=CHAT, week=old_week, question_id=9, first_at=NOW, correct=True),
            Day(chat_id=CHAT, day=old_day, scored=5, exam_bonus=0),
            Score(chat_id=CHAT, week=old_week, points=44, seed=3),
            Slot(chat_id=CHAT, week=WEEK, question_id=8, first_at=NOW, correct=True),
        ])
        await s.commit()

    async with api_db() as s:
        removed = await league.prune(s, keep_seasons=2, now=NOW)
        await s.commit()
    assert removed == 2, f"expected the old slot and the old day to go, removed {removed}"

    async with api_db() as s:
        assert await s.get(Slot, (CHAT, old_week, 9)) is None
        assert await s.get(Day, (CHAT, old_day)) is None
        assert await s.get(Slot, (CHAT, WEEK, 8)) is not None, "this season was pruned"
        kept = await s.get(Score, (CHAT, old_week))
    assert kept is not None and kept.points == 44, \
        "the running total was pruned — that is the history the board reads"
