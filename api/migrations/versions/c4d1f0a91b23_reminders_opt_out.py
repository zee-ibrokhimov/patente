"""reminders opt-out

A learner can stop the come-back nudges without blocking the bot, which is the only other
way they had to make them stop — and blocking takes the payment notices and the renewal
warning with it.

server_default '0' rather than a bare NOT NULL: the table has rows, and adding a
non-nullable column without one fails on the existing six. The model-side `default=False`
covers inserts from Python; this covers the rows already there.

Revision ID: c4d1f0a91b23
Revises: 87c8dcf00671
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4d1f0a91b23"
down_revision: Union[str, Sequence[str], None] = "87c8dcf00671"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("reminders_off", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("users", "reminders_off")
