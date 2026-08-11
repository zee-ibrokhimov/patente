"""This week's league table.

Monday to Sunday in UTC, ranked on CORRECT answers.

WEEKLY, NOT ALL-TIME
An all-time board is won permanently by whoever arrived first. Nobody joining later can ever
place, so for every user after the first few it is a screen showing that they have already
lost — the exact opposite of the mechanic. A fixed week also supplies the deadline that makes
a league work, and it needs no reset job: "this week" is a WHERE clause on the answer log.

CORRECT ANSWERS, NOT ANSWERS GIVEN
Counting attempts rewards tapping fast and being wrong. On a product whose entire pitch is
understanding the question rather than memorising the key, that would rank the behaviour it
exists to prevent — and it is trivially farmable by holding down VERO.

THE PRIVACY PART, WHICH IS THE PART TO BE CAREFUL WITH
This is the ONLY place in the product where one user's personal data is shown to another.
Three rules, all enforced here rather than in the route, so a second caller cannot get them
wrong:

  · someone who opted out is absent from the ranking ENTIRELY, not merely hidden — they do
    not occupy a place, and removing them does not leave a gap for others to infer;
  · nothing beyond a first name and a score ever leaves this module. No chat id, no
    username, no language, no streak, nothing that would let one learner find another;
  · a learner with no name recorded is shown as a neutral placeholder rather than skipped,
    because skipping them would make the ranks lie.

THE SMALL-N PROBLEM, STATED RATHER THAN HIDDEN
With four users somebody is permanently last and everybody's position is fixed. That is
demoralising, not motivating. The API reports `ranked` so the client can say "not enough
people yet" instead of rendering a competition between three people — see
LEADERBOARD_MIN_PLAYERS. The rows are still returned: hiding the data would make the feature
untestable and the owner unable to see their own product working.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import LeagueScore, User
from shared.constants import (
    LEADERBOARD_SIZE,
    LEAGUE_MEDAL_PLACES,
    LEAGUE_MIN_POINTS,
    LEAGUE_PRIZE_MIN_RANKED,
)


def week_start(today: date | None = None) -> datetime:
    """Midnight UTC on the Monday of the current week.

    UTC rather than the learner's timezone, and deliberately: the league has to be the same
    week for everyone in it, or two people comparing positions are comparing different
    windows. The cost is that the reset lands mid-evening for some — acceptable, and the
    alternative is a per-user week that cannot be ranked.
    """
    today = today or datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return datetime.combine(monday, time.min, tzinfo=timezone.utc)


async def _ranked_rows(session: AsyncSession, week: str, limit: int | None = None):
    """The season's ranked learners, best first. One indexed query.

    The three rules from the module docstring live here and nowhere else:

      · opted out means ABSENT — a live join against `users`, never a copy of the flag on
        the score row, because the opt-out has to work retroactively and a denormalised copy
        would need every row rewritten to honour that;
      · nothing but a first name and a score is selected, so nothing else can leak;
      · below LEAGUE_MIN_POINTS nobody holds a rank at all. One correct answer used to
        occupy a place, which is absurd on a board that now carries medals.

    Ordered by points, then by `seed` — a per-row random number, NOT chat_id. Ties are the
    normal case under a daily ceiling, and ordering them by Telegram id would hand every
    medal to the oldest account and publish the population's signup order.
    """
    stmt = (
        select(LeagueScore.chat_id, User.display_name, LeagueScore.points)
        .join(User, User.chat_id == LeagueScore.chat_id)
        .where(
            LeagueScore.week == week,
            LeagueScore.points >= LEAGUE_MIN_POINTS,
            User.leaderboard_opt_out.is_(False),
        )
        .order_by(LeagueScore.points.desc(), LeagueScore.seed)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return (await session.execute(stmt)).all()


async def _ranked_count(session: AsyncSession, week: str) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(LeagueScore)
        .join(User, User.chat_id == LeagueScore.chat_id)
        .where(
            LeagueScore.week == week,
            LeagueScore.points >= LEAGUE_MIN_POINTS,
            User.leaderboard_opt_out.is_(False),
        )
    ) or 0


async def _medals(session: AsyncSession, previous_week: str) -> dict[int, int]:
    """Last season's podium: chat_id -> 1, 2 or 3.

    COMPUTED LIVE, NEVER STORED. A medals table would have to hold a name or a chat id, and
    it would then survive both `/delete` and the opt-out — for exactly the three most visible
    learners on the board. The cost of computing it is that a closed season can change: a
    learner who opts out on Monday takes their medal with them, and if they were the tenth
    ranked learner the season stops being awardable for everyone else.

    That is the honest trade and it is the right way round. The retroactive opt-out is the
    promise this product actually made — in the model, in the settings screen and in a test —
    and durable medals would quietly break it.

    THE IMMEDIATELY PRECEDING SEASON ONLY. A stack of six medals beside one name rebuilds the
    all-time board, which is a screen that tells every newcomer they have already lost.
    """
    if await _ranked_count(session, previous_week) < LEAGUE_PRIZE_MIN_RANKED:
        # Too quiet a week to have awarded anything. Distinct from LEADERBOARD_MIN_PLAYERS,
        # which is only about whether the CLIENT draws it as a competition.
        return {}
    rows = await _ranked_rows(session, previous_week, limit=LEAGUE_MEDAL_PLACES)
    return {chat_id: place for place, (chat_id, _name, _points) in enumerate(rows, 1)}


async def board(
    session: AsyncSession, user: User, today: date | None = None
) -> dict:
    """The top of the table, plus where the caller stands.

    Their own row is returned even when they are outside the top — "you are 34th with 12"
    is information; a board they cannot find themselves on is just other people.

    Four indexed queries, and the count is bounded: it does not grow with the population,
    which is the entire reason the running total in `league_score` exists. The version this
    replaced loaded every answer event of the week into Python on every view.
    """
    since = week_start(today)
    week = since.date().isoformat()
    previous = (since.date() - timedelta(days=7)).isoformat()

    rows = await _ranked_rows(session, week, limit=LEADERBOARD_SIZE)
    ranked = await _ranked_count(session, week)
    medals = await _medals(session, previous)

    mine = await session.get(LeagueScore, (user.chat_id, week))
    my_points = mine.points if mine else 0

    # Rank by counting who is ahead, rather than by scanning the board — the caller is very
    # often outside the top fifteen, and "34th" has to be right without loading 34 rows.
    my_rank: int | None = None
    if mine is not None and my_points >= LEAGUE_MIN_POINTS and not user.leaderboard_opt_out:
        ahead = await session.scalar(
            select(func.count())
            .select_from(LeagueScore)
            .join(User, User.chat_id == LeagueScore.chat_id)
            .where(
                LeagueScore.week == week,
                LeagueScore.points >= LEAGUE_MIN_POINTS,
                User.leaderboard_opt_out.is_(False),
                or_(LeagueScore.points > my_points,
                    and_(LeagueScore.points == my_points, LeagueScore.seed < mine.seed)),
            )
        ) or 0
        my_rank = ahead + 1

    return {
        "week_start": since,
        "ranked": ranked,
        # Whether a season this size would award anything. Sent so the rules screen can stop
        # promising a prize on a board of four, and separate from the client's own "too quiet
        # to draw" threshold, which is a different number for a different reason.
        "prize_eligible": ranked >= LEAGUE_PRIZE_MIN_RANKED,
        "entries": [
            {
                "rank": position,
                "name": name,
                "score": points,
                "is_me": chat_id == user.chat_id,
                # Its own field, and on the client its own element — never concatenated into
                # the name. Telegram names are unfiltered, so someone calling themselves
                # "\U0001f947 Aziz" would otherwise appear to be wearing a medal they never won.
                "medal": medals.get(chat_id),
            }
            for position, (chat_id, name, points) in enumerate(rows, 1)
        ],
        "me": {
            "rank": my_rank,
            # Shown even when they are opted out or below the floor. The running total makes
            # this free, and telling somebody who answered fifty questions that their score
            # is zero — which is what the old board did — is simply wrong.
            "score": my_points,
            # Someone who opted out is told so rather than shown an empty board and left to
            # wonder whether the feature is broken.
            "opted_out": user.leaderboard_opt_out,
            "medal": medals.get(user.chat_id),
        },
    }
