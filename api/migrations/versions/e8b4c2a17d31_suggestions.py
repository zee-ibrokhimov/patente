"""suggestions — a form, not a chat link

The first version of "what should we add?" opened the support conversation. That was the
wrong trade and the owner said so: a chat link asks a learner to compose a message to a
stranger, which almost nobody does, and it puts every suggestion in the same inbox as
"my payment failed".

A form asks for one thing, in the language the app is already in, and lands somewhere the
owner can read in a list.

`handled_at` rather than a status enum: there are exactly two states a suggestion is ever
in — read or not — and the reports queue next to it already works this way. Anything richer
is a workflow nobody asked for.

Revision ID: e8b4c2a17d31
Revises: d7a3e1c95f04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8b4c2a17d31"
down_revision: Union[str, Sequence[str], None] = "d7a3e1c95f04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("text", sa.Text(), nullable=False),
        # The interface language at the time of writing. The owner reads four languages'
        # worth of these and needs to know which one a message is in before opening it.
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP"), index=True),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("suggestions")
