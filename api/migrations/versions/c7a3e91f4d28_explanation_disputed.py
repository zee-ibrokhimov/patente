"""record which statements the model disagreed with, per question

Measured on *Segnali di precedenza*: 8 of 12 clusters were flagged because the model
disagreed with the ministerial answer on 1-3 statements out of 12 — and in every case
the explanation of the rule was correct. The disagreements were about derived facts the
article does not state ("perde efficacia in presenza di agente che regola il traffico"),
not about the rule being explained.

Withholding the whole cluster for that took the servable share of the topic down to
5 of 15. Recording the disputed question ids lets the explanation serve the statements
it agrees about and be withheld only for the ones it does not, which is both safer and
far more available: a user never sees an explanation contradicting the answer they were
just shown, and the other ten statements in the cluster still get explained.

Revision ID: c7a3e91f4d28
Revises: b4e2f83c17a9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7a3e91f4d28"
down_revision = "b4e2f83c17a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("explanations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("disputed", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("explanations", schema=None) as batch_op:
        batch_op.drop_column("disputed")
