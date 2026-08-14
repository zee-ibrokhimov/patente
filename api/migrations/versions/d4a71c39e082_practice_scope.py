"""what a practice sitting was drawn from

NULL means the whole bank, which is what every sitting that already exists was drawn from and
what a learner who taps Start without choosing still gets. So no backfill: NULL is not a gap
here, it is the honest answer for those rows and the correct default for new ones.

Otherwise a family key from TOPIC_FAMILIES, or "topic:<id>" for one ministerial topic. Parsed
in exactly one place — api/services/categories.py — so a scope that reaches the question draw
is one that was already known to exist.

EXAMS ARE ALWAYS NULL, enforced in `quiz_sessions.create` rather than by a constraint. A
simulator weighted toward chosen topics reports a score that means nothing, which is the same
argument `selection.exam_paper` already makes for its uniform draw.

Revision ID: d4a71c39e082
Revises: c9f4b21e8d76
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a71c39e082"
down_revision: Union[str, Sequence[str], None] = "c9f4b21e8d76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Nullable, so no server_default is needed and no table rewrite happens — the trap
    # f31a86a33e7d documents applies to NOT NULL columns, and this one is genuinely optional.
    op.add_column("quiz_sessions", sa.Column("scope", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("quiz_sessions", "scope")
