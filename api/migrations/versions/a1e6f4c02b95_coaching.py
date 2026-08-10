"""stored study advice, so a second tap does not pay twice

The analysis is the FIRST AI cost in this product that scales with users and can never be
shared between them, because it is about them. Every other one — 3,382 explanations, 7,106
translations — is capped by content and cached forever, so the marginal cost of one more
learner is zero. This one has no ceiling except the cooldown.

So it is stored, not streamed and forgotten: within the cooldown the same body is handed
back, which makes reopening the screen free and makes the cooldown enforceable by looking
at what is already there rather than by a separate counter that can drift from it.

`lang` is recorded but is NOT part of the cooldown key. As originally designed the language
was checked first, so four taps in Settings bought four analyses inside one window — and
one account could have driven roughly 360 calls an hour on a EUR 2.99 subscription. The
cooldown is per ACCOUNT and language is a detail underneath it.

Revision ID: a1e6f4c02b95
Revises: f3c9d5b81a72
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1e6f4c02b95"
down_revision: Union[str, Sequence[str], None] = "f3c9d5b81a72"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        # Logged on every row so a silent revert to an expensive configuration is visible.
        # translations.py records a case where a parameter was dropped by a retry and
        # nobody noticed for weeks, at 5-10x the cost.
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_analysis_chat_time", "analyses", ["chat_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_analysis_chat_time", table_name="analyses")
    op.drop_table("analyses")
