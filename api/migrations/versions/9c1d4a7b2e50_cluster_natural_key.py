"""give clusters a stable identity

Adding this while `clusters` is still empty is nearly free; adding it after
step 7 would mean reconciling ids against explanations that already exist.

The cluster id was positional — cluster.py rebuilt every row and numbered them
by sort order — while `explanations.cluster_id` is ON DELETE CASCADE and the
connection sets PRAGMA foreign_keys=ON. Re-running the clustering step after
generating explanations therefore deleted all of them. `natural_key` identifies
a cluster by the figure or statement it is about, so a rerun can match a rule to
the row that already explains it instead of renumbering underneath it.

Revision ID: 9c1d4a7b2e50
Revises: 4bafdede07a8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c1d4a7b2e50"
down_revision = "4bafdede07a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Backfilled with the id for any row that predates this migration. Nothing
    # ships with clusters populated, but a developer database might, and a NOT
    # NULL unique column needs every existing row to carry a distinct value.
    with op.batch_alter_table("clusters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("natural_key", sa.Text(), nullable=True))
    op.execute("UPDATE clusters SET natural_key = 'legacy:' || id WHERE natural_key IS NULL")
    with op.batch_alter_table("clusters", schema=None) as batch_op:
        batch_op.alter_column("natural_key", existing_type=sa.Text(), nullable=False)
        batch_op.create_index(batch_op.f("ix_clusters_natural_key"), ["natural_key"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("clusters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_clusters_natural_key"))
        batch_op.drop_column("natural_key")
