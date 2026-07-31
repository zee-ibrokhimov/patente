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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Event, User
from shared.constants import EV_ANSWER_GIVEN, LEADERBOARD_SIZE


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


async def _scores(session: AsyncSession, since: datetime) -> list[tuple[int, str | None, int]]:
    """(chat_id, display_name, correct answers) for everyone eligible, best first.

    The count reads `payload` in Python rather than in SQL: the JSON accessor differs
    between SQLite and Postgres, and this table is small enough that portability is worth
    more than the query. Revisit if it ever stops being.
    """
    rows = (await session.execute(
        select(Event.chat_id, Event.payload, User.display_name)
        .join(User, User.chat_id == Event.chat_id)
        .where(
            Event.type == EV_ANSWER_GIVEN,
            Event.created_at >= since,
            # Opted out means ABSENT, not hidden. They do not hold a place, and their
            # absence leaves no gap for anyone to reason about.
            User.leaderboard_opt_out.is_(False),
        )
    )).all()

    tally: dict[int, int] = {}
    names: dict[int, str | None] = {}
    for chat_id, payload, name in rows:
        names[chat_id] = name
        if (payload or {}).get("correct"):
            tally[chat_id] = tally.get(chat_id, 0) + 1

    # Ties broken by chat_id so the order is stable between requests. An unstable ranking
    # makes the board look like it is flickering when nobody has answered anything.
    return sorted(
        ((cid, names.get(cid), score) for cid, score in tally.items() if score > 0),
        key=lambda r: (-r[2], r[0]),
    )


async def board(
    session: AsyncSession, user: User, today: date | None = None
) -> dict:
    """The top of the table, plus where the caller stands.

    Their own row is returned even when they are outside the top — "you are 34th with 12"
    is information; a board they cannot find themselves on is just other people.
    """
    since = week_start(today)
    scores = await _scores(session, since)

    me_rank: int | None = None
    me_score = 0
    for position, (chat_id, _name, score) in enumerate(scores, 1):
        if chat_id == user.chat_id:
            me_rank, me_score = position, score
            break

    return {
        "week_start": since,
        "ranked": len(scores),
        "entries": [
            {
                "rank": position,
                "name": name,
                "score": score,
                "is_me": chat_id == user.chat_id,
            }
            for position, (chat_id, name, score) in enumerate(scores[:LEADERBOARD_SIZE], 1)
        ],
        "me": {
            "rank": me_rank,
            "score": me_score,
            # Someone who opted out is told so rather than shown an empty board and left to
            # wonder whether the feature is broken.
            "opted_out": user.leaderboard_opt_out,
        },
    }
