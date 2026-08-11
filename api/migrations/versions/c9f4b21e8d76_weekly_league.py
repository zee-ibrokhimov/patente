"""the weekly league: a running total, and the two ledgers that bound it

The board this replaces loaded every answer event of the week into Python and decoded each
one, on every single view — so the work was weekly-answers x viewers and grew with the square
of the user base. Measured at roughly a second in raw SQLite at 2,000 active learners, two to
four seconds through the app, while holding one of only fifteen connections.

Expressing the NEW rules over the event log is worse, not better: one point per question per
week means deduplicating across that learner's whole week on every view. So the total is kept
as points are scored, and the board becomes an index seek.

THREE TABLES, BECAUSE THERE ARE THREE DIFFERENT KEYS

  league_slot   (chat_id, week, question_id)  — this question is spent for this season
  league_day    (chat_id, day)                — both daily ceilings
  league_score  (chat_id, week)               — the running total the board reads

Collapsing any pair means one of them stops being a primary key and becomes a query.

ALL THREE CARRY ON DELETE CASCADE, unlike `streak_days`, and that is the point of this note.
`delete_user` anonymises events rather than deleting them, so every ledger derived from the
log resets on erasure. A ledger in its own table keyed on chat_id does not — and chat_id is
the permanent Telegram id, so the same person returns to a season in which every question is
already spent.

WHAT IS BACKFILLED, AND WHAT IS DELIBERATELY NOT

The CURRENT week only, in SQL. Not the whole log, for two reasons. The `streak_days` backfill
one revision back used `.fetchall()` over every answer event and `json.loads` per row, which
is fine at 255 rows and is 0.42 GB of resident memory at 340,000 — that shape is not repeated
here. And older seasons cannot be replayed honestly anyway:

  · `credited` is absent from 223 of the 255 answer events in production, because pacing
    shipped after them. The house rule elsewhere is "absent means credited", and it is used
    here too — but note what it means: it credits, blind, exactly the period in which the
    answer endpoint had no pacing rule at all.
  · Nothing records whether an answer was informed by the answer key that endpoint used to
    give away. There is no discriminator and there never can be.

So no medal is awarded for any season that predates these rules, and only the live week is
seeded. Replaying the agreed rules over the whole production log yields one ranked learner
sitting exactly on the twenty-point floor — one point of drift takes it to zero — which is
not a result worth freezing into a table.

`chat_id IS NOT NULL` in the backfill is mandatory, not defensive: `delete_user` leaves
anonymised event rows behind, and unlike the old board there is no accidental inner join here
to filter them out.

Rows are written for opted-out learners too, and filtered at read time. Otherwise opting back
in would silently lose the week.

Revision ID: c9f4b21e8d76
Revises: b5d82f10c4ae
"""

import random
from datetime import datetime, time, timedelta, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9f4b21e8d76"
down_revision: Union[str, Sequence[str], None] = "b5d82f10c4ae"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Duplicated rather than imported, the rule this project's migrations follow: a migration is
# a record of what the database did on the day it ran, and importing live constants would
# make this file silently re-mean something the next time a rule is tuned.
DAILY_ANSWER_CAP = 40


def _week_of(moment: datetime) -> str:
    day = moment.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def upgrade() -> None:
    op.create_table(
        "league_slot",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("week", sa.Text(), nullable=False),
        sa.Column("question_id", sa.Integer(), nullable=False),
        sa.Column("first_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "week", "question_id"),
        sa.ForeignKeyConstraint(["chat_id"], ["users.chat_id"], ondelete="CASCADE"),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "league_day",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("day", sa.Text(), nullable=False),
        # server_default on every NOT NULL integer: adding one without it to a live table is
        # the trap f31a86a33e7d documents.
        sa.Column("scored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exam_bonus", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("chat_id", "day"),
        sa.ForeignKeyConstraint(["chat_id"], ["users.chat_id"], ondelete="CASCADE"),
        sqlite_with_rowid=False,
    )
    op.create_table(
        "league_score",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("week", sa.Text(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "week"),
        sa.ForeignKeyConstraint(["chat_id"], ["users.chat_id"], ondelete="CASCADE"),
        sqlite_with_rowid=False,
    )
    # Not optional. The primary key leads with chat_id, so `WHERE week = ?` cannot seek, and
    # the table gains a row per active learner per week forever. Measured at 52 seasons
    # banked: 10.7 ms per board view without it, 1.5 ms with — and the multiplier is the
    # number of weeks since launch, so it benchmarks perfectly in week one.
    op.create_index("ix_league_score_week_points", "league_score", ["week", "points"])

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    week = _week_of(now)
    monday = datetime.combine(
        datetime.fromisoformat(week).date(), time.min, tzinfo=timezone.utc
    ).replace(tzinfo=None).isoformat(sep=" ")

    # One slot per (learner, question) for this season, stamped with the FIRST answer — and
    # `correct` taken from that same first answer. SQLite's bare-column rule makes the
    # non-aggregated columns come from the row that supplied min(created_at); a test asserts
    # that rather than trusting it.
    bind.execute(sa.text(f"""
        INSERT INTO league_slot (chat_id, week, question_id, first_at, correct)
        SELECT chat_id,
               :week,
               json_extract(payload, '$.question_id'),
               min(created_at),
               CASE WHEN json_extract(payload, '$.correct') IN (1, 'true') THEN 1 ELSE 0 END
        FROM events
        WHERE type = 'answer_given'
          AND chat_id IS NOT NULL
          AND json_extract(payload, '$.question_id') IS NOT NULL
          AND created_at >= :monday
          AND coalesce(json_extract(payload, '$.credited'), 1) IS 1
        GROUP BY chat_id, json_extract(payload, '$.question_id')
    """), {"week": week, "monday": monday})

    # The daily ceiling, applied by ranking each learner's correct slots within their UTC day
    # and keeping the first {cap}. Same rule the service enforces with an upsert, expressed
    # once here as a window function.
    bind.execute(sa.text(f"""
        INSERT INTO league_day (chat_id, day, scored, exam_bonus)
        SELECT chat_id, day, count(*), 0
        FROM (
            SELECT chat_id,
                   date(first_at) AS day,
                   row_number() OVER (
                       PARTITION BY chat_id, date(first_at) ORDER BY first_at
                   ) AS n
            FROM league_slot
            WHERE week = :week AND correct = 1
        )
        WHERE n <= {DAILY_ANSWER_CAP}
        GROUP BY chat_id, day
    """), {"week": week})

    rows = bind.execute(sa.text(
        "SELECT chat_id, sum(scored) FROM league_day GROUP BY chat_id"
    )).fetchall()
    if rows:
        bind.execute(sa.text(
            "INSERT INTO league_score (chat_id, week, points, seed) "
            "VALUES (:chat_id, :week, :points, :seed)"
        ), [{"chat_id": chat_id, "week": week, "points": points,
             # Seeded here for the same reason the service seeds it: the tiebreak must not be
             # chat_id, or every tie goes to the oldest account forever.
             "seed": random.getrandbits(31)}
            for chat_id, points in rows if points])

    # No exam bonuses are backfilled. `EV_EXAM_FINISHED` carries no timestamp of its own and
    # its `created_at` is when the sitting was DISCOVERED to be over — gaps of 46 and 61
    # minutes exist in production — so the day it belongs to cannot be recovered. Zero exams
    # have ever been passed, so the honest answer and the convenient one agree.


def downgrade() -> None:
    op.drop_index("ix_league_score_week_points", table_name="league_score")
    op.drop_table("league_score")
    op.drop_table("league_day")
    op.drop_table("league_slot")
