"""record why a draft explanation was flagged

`generate.py` withholds the ministerial answer from the model and compares it
afterwards, so a flagged draft can mean the explanation is wrong, the topic is
mapped to the wrong article, or the answer key itself is wrong. Those are three
different investigations and the reviewer needs to know which one they are in.

Revision ID: b4e2f83c17a9
Revises: 9c1d4a7b2e50
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4e2f83c17a9"
down_revision = "9c1d4a7b2e50"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("explanations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("flags", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("explanations", schema=None) as batch_op:
        batch_op.drop_column("flags")
