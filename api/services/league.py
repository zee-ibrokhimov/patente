"""Earning a point. The write half of the weekly league; `leaderboard.py` is the read half.

Split from `leaderboard.py` on purpose. That module's docstring holds the privacy rules —
who may appear, what may leave the server — and those rules are enforced in exactly one
place. This module never reads a name, never reads another learner's row, and never decides
who is visible. It only decides whether something that just happened is worth a point.

THE RULES, AND WHY EACH ONE IS A REFUSAL

  · One point per QUESTION per week, taken on the first answer to it. Not per answer: a
    repeat round is an unlimited stream of questions the learner already knows the answer
    to, an exam re-serves questions they have seen, and practice hands a missed question
    back after ten minutes. All three are ordinary features and all three would otherwise
    be scoring engines.
  · The first answer spends the slot even when it is WRONG. Refunding it would make
    guess-then-retry optimal, which is the behaviour this product exists to discourage.
  · Only credited answers score, and an uncredited one spends nothing. A sub-second blitz
    therefore gains its author nothing and costs them nothing — the questions are still
    there to be earned properly later. The opposite rule would let a blitz permanently burn
    a week of a learner's own questions, and one real sitting in the log answered 23
    questions in 85 seconds.
  · At most LEAGUE_DAILY_ANSWER_CAP scoring answers a UTC day, enforced by the database.
  · Passing a mock exam pays LEAGUE_EXAM_BONUS, at most once a UTC day.

NO SELECT ANYWHERE ON THIS PATH

Every decision is an INSERT whose own return value is the answer: `on_conflict_do_nothing`
tells us whether the slot was already spent, and `ON CONFLICT DO UPDATE ... WHERE scored <
cap` tells us whether the day was already full. So there is no read-then-write window for
two concurrent answers to slip through, and nothing here gets slower as a learner's history
grows. The cost is one primary-key insert per credited answer and at most three statements
in the scoring case.

THIS MODULE COMMITS TO SQLITE, and says so rather than pretending otherwise: it imports the
SQLite dialect for `ON CONFLICT`. `leaderboard.py` hedges towards Postgres in a comment; that
hedge does not survive contact with this file, and a design that is honest about its database
is easier to port than one that is vague about it.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import LeagueDay, LeagueScore, LeagueSlot
from shared.constants import (
    LEAGUE_DAILY_ANSWER_CAP,
    LEAGUE_EXAM_BONUS,
    LEAGUE_POINTS_PER_ANSWER,
)

log = logging.getLogger(__name__)


def week_of(moment: datetime) -> str:
    """The season key: the ISO date of that week's Monday, in UTC.

    NOT `strftime('%Y-%W')` and NOT `(year, isocalendar week)`. The first splits Monday
    2025-12-29 into '2025-52' and '2026-00'; the second collides, because that Monday is ISO
    2026-W01 inside calendar year 2025. Both were run before this was written down.

    A Monday date has neither problem, and it is the same value `leaderboard.week_start`
    already returns — so the key rows are stored under and the `week_start` the API reports
    cannot drift apart.
    """
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def utc_day(moment: datetime) -> str:
    """The day both daily ceilings are counted in.

    UTC, unlike the streak's Rome day, and the two genuinely differ. A UTC week contains
    eight distinct Rome dates, so a Rome day would let the boundary Monday's cap straddle two
    seasons and would raise the weekly ceiling from 280 to 320. The price is that the cap
    rolls over at 02:00 in Rome; that is a smaller wrong than a cap leaking across a season.
    """
    moment = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).date().isoformat()


def _monday(day: date) -> datetime:
    return datetime.combine(day - timedelta(days=day.weekday()), time.min,
                            tzinfo=timezone.utc)


async def _add_points(session: AsyncSession, chat_id: int, week: str, points: int) -> None:
    """Add to the running total, creating the row if this is their first point of the season.

    `seed` is written once, when the row is created, and is the tiebreak. Random rather than
    chat_id: ordering ties by Telegram id hands every tie to the oldest account, and under a
    daily ceiling exact ties are the normal case — so the same accounts would take the medals
    every week, and the board would leak a total ordering of its population by signup date.
    """
    await session.execute(
        sqlite_insert(LeagueScore)
        .values(chat_id=chat_id, week=week, points=points,
                seed=random.getrandbits(31))
        .on_conflict_do_update(
            index_elements=["chat_id", "week"],
            set_={"points": LeagueScore.points + points},
        )
    )


async def score_answer(
    session: AsyncSession, chat_id: int, question_id: int, correct: bool,
    now: datetime | None = None,
) -> bool:
    """Score one credited answer. Returns whether it was worth a point.

    Called from `answers.record_answer` AFTER the event is written, and short-circuited on
    `credited` by the caller — the same shape as the streak hook beside it, for the same
    reason: a repair or a replay must be able to find the answer in the log.
    """
    now = now or datetime.now(timezone.utc)
    week = week_of(now)

    # 1. Claim the question for the week. This is the ONLY statement that runs on every
    #    credited answer, and it is a primary-key insert into a WITHOUT ROWID table.
    claimed = await session.execute(
        sqlite_insert(LeagueSlot)
        .values(chat_id=chat_id, week=week, question_id=question_id,
                first_at=now, correct=correct)
        .on_conflict_do_nothing()
    )
    if not claimed.rowcount:
        return False                      # already answered this question this week
    if not correct:
        return False                      # the slot is spent; see the module docstring

    # 2. Charge the daily cap. The WHERE clause is what enforces it: rowcount is 1 while
    #    under the cap and 0 once at it, so the cap cannot be raced past and no COUNT is
    #    needed. An answer past the cap has still spent its slot — accepted, because
    #    releasing it means a DELETE on the hot path and reopens "answer everything cheaply
    #    now, re-answer for points later".
    charged = await session.execute(
        sqlite_insert(LeagueDay)
        .values(chat_id=chat_id, day=utc_day(now), scored=1)
        .on_conflict_do_update(
            index_elements=["chat_id", "day"],
            set_={"scored": LeagueDay.scored + 1},
            where=LeagueDay.scored < LEAGUE_DAILY_ANSWER_CAP,
        )
    )
    if not charged.rowcount:
        return False                      # capped for today

    await _add_points(session, chat_id, week, LEAGUE_POINTS_PER_ANSWER)
    return True


async def score_exam_pass(
    session: AsyncSession, chat_id: int, finished_at: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Pay the mock-exam bonus, at most once a UTC day. Returns whether it paid.

    THE DAY AND THE SEASON COME FROM `finished_at`, NOT FROM NOW, because an exam is graded
    whenever somebody next looks at it. A sitting that ran out of time on Sunday evening can
    be discovered on Tuesday — the gap between the two has been observed at over an hour in
    the existing data — and paying it into Tuesday would put points in a season the work did
    not happen in.

    But a CLOSED season must stay closed: medals have been shown for it and, once there are
    prizes, awarded from it. So a pass whose week is no longer the current one pays nothing
    at all. That is a real cost — a rare pass discovered after the Monday boundary is never
    paid — and it is logged so support can explain it rather than guess.

    THIS FUNCTION MUST NOT RAISE. It runs inside the exam-grading transaction, and unlike
    `events.record` there is nothing here that swallows exceptions: a failure would roll back
    the grade itself and lose the learner's result. Two upserts, no reads, no parsing, and
    deliberately no touching of the lazy `row.user` relationship, which raises under asyncio.
    """
    now = now or datetime.now(timezone.utc)
    if finished_at is None:
        return False
    week = week_of(finished_at)
    if week != week_of(now):
        log.info("exam pass for %s not paid: finished in season %s, now in %s",
                 chat_id, week, week_of(now))
        return False

    awarded = await session.execute(
        sqlite_insert(LeagueDay)
        .values(chat_id=chat_id, day=utc_day(finished_at), exam_bonus=1)
        .on_conflict_do_update(
            index_elements=["chat_id", "day"],
            set_={"exam_bonus": 1},
            where=LeagueDay.exam_bonus == 0,
        )
    )
    if not awarded.rowcount:
        return False                      # already paid for a pass today

    await _add_points(session, chat_id, week, LEAGUE_EXAM_BONUS)
    return True


async def prune(session: AsyncSession, keep_seasons: int, now: datetime | None = None) -> int:
    """Drop the per-question and per-day ledgers of long-finished seasons.

    `league_score` is one small row per learner per week and is kept forever — it is the
    history. The two tables underneath it are one row per QUESTION per learner per week, and
    at ten thousand learners that is about a gigabyte a year on a host with six free. They
    answer "why do I have 34 points", which nobody asks about a season two months gone.

    Belongs to the janitor, never to a request.
    """
    from sqlalchemy import delete

    now = now or datetime.now(timezone.utc)
    cutoff = (_monday(now.astimezone(timezone.utc).date())
              - timedelta(weeks=keep_seasons)).date().isoformat()
    removed = 0
    for model in (LeagueSlot, LeagueDay):
        column = model.week if model is LeagueSlot else model.day
        result = await session.execute(delete(model).where(column < cutoff))
        removed += result.rowcount or 0
    if removed:
        log.info("pruned %s league ledger rows from before %s", removed, cutoff)
    return removed
