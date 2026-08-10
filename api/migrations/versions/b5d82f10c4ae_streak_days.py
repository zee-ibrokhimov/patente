"""the days a learner actually met the daily goal

The streak used to mean "answered something", which was one grouped query over the event log
and needed no table. It now means ten distinct questions at a credited pace, no sooner than
eight hours after the previous qualifying day — a forward walk over a learner's entire answer
history, which is the same shape as the leaderboard scan that cannot carry its own traffic.
So the expensive part is computed once, when it becomes true, and the profile reads dates.

THE BACKFILL IS THE WHOLE POINT OF THIS MIGRATION

Without it every existing streak resets to zero on deploy, because the table would start
empty and history would be unreadable under the new rule. So the same rule is replayed over
the event log here. It is affordable precisely once: 254 answer events exist today.

`credited` is absent from events written before pacing shipped, and those are COUNTED. The
alternative — absent means uncredited — would erase the history of everyone who was here
before the rule existed, to protect against farming that was not possible at the time.

Revision ID: b5d82f10c4ae
Revises: a1e6f4c02b95
"""

import json
from datetime import date, datetime, timedelta, timezone
from typing import Sequence, Union
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from alembic import op

revision: str = "b5d82f10c4ae"
down_revision: Union[str, Sequence[str], None] = "a1e6f4c02b95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Duplicated from api/services/streak.py on purpose. A migration is a historical record of
# what the database did on the day it ran; importing the live constants would make this file
# silently re-mean something the next time the goal is tuned.
ROME = ZoneInfo("Europe/Rome")
GOAL = 10
MIN_GAP = timedelta(hours=8)


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _rome_day(moment: datetime) -> date:
    return moment.astimezone(ROME).date()


def upgrade() -> None:
    op.create_table(
        "streak_days",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        # A Rome calendar day, as text. Not a timestamp: it is a civil date, and storing it
        # as an instant would reintroduce exactly the timezone question the column exists to
        # settle.
        sa.Column("day", sa.Text(), nullable=False),
        sa.Column("qualified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("questions", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("chat_id", "day"),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT chat_id, created_at, payload FROM events "
        "WHERE type = 'answer_given' AND chat_id IS NOT NULL "
        "ORDER BY chat_id, created_at"
    )).fetchall()

    # chat -> day -> ordered timestamps at which a NEW distinct question was answered.
    seen: dict[int, dict[date, list[datetime]]] = {}
    firsts: dict[int, dict[date, set[int]]] = {}
    for chat_id, created_at, payload in rows:
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        if data.get("credited") is False:
            continue
        question_id = data.get("question_id")
        if question_id is None:
            continue
        when = _parse(created_at)
        day = _rome_day(when)
        done = firsts.setdefault(chat_id, {}).setdefault(day, set())
        if question_id in done:
            continue
        done.add(question_id)
        seen.setdefault(chat_id, {}).setdefault(day, []).append(when)

    inserted = []
    for chat_id, by_day in seen.items():
        previous: datetime | None = None
        for day in sorted(by_day):
            stamps = by_day[day]
            if len(stamps) < GOAL:
                continue
            # The first moment at or after the tenth distinct question that also clears the
            # minimum gap — the same "try again on every later answer" rule the service uses,
            # so a day finished early is earned later rather than lost.
            moment = next(
                (s for s in stamps[GOAL - 1:]
                 if previous is None or s - previous >= MIN_GAP),
                None,
            )
            if moment is None:
                continue
            previous = moment
            inserted.append({
                "chat_id": chat_id,
                "day": day.isoformat(),
                "qualified_at": moment.astimezone(timezone.utc).replace(tzinfo=None),
                "questions": sum(1 for s in stamps if s <= moment),
            })

    if inserted:
        bind.execute(sa.text(
            "INSERT INTO streak_days (chat_id, day, qualified_at, questions) "
            "VALUES (:chat_id, :day, :qualified_at, :questions)"
        ), inserted)


def downgrade() -> None:
    op.drop_table("streak_days")
