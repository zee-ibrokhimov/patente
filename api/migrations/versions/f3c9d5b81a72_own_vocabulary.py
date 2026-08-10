"""a learner's own vocabulary, alongside the shared glossary

NULL owner means the shared list — every row that exists today. A chat id means one
learner's private word, visible to nobody else.

WHY ONE TABLE AND NOT TWO
A second table would need its own progress rows, its own Leitner scheduling, its own place
in the round draw and in the flip-card deck, and every one of those is a chance for the two
kinds of word to behave differently. Sharing the table means a learner's own words are drawn,
scheduled, graded and counted by exactly the code that already works. The cost is that every
query must be scoped, which is why there is one helper for it and a test that fails if a
query is written without it.

TWO UNIQUENESS RULES, NOT ONE
`uq_vocab_term_it` made `it` unique across the whole table, which would stop a learner adding
a word the shared glossary already has — and they may well want their own note on `sosta`.
It is replaced by:

  · a PARTIAL unique index over the shared rows only, which is the old rule exactly;
  · a unique index on (owner_chat_id, it), so one learner cannot add the same word twice.

Both are needed. A plain UNIQUE(owner_chat_id, it) would not preserve the first, because
SQLite treats NULLs as distinct and would happily accept two shared rows for `sosta`.

Revision ID: f3c9d5b81a72
Revises: e8b4c2a17d31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3c9d5b81a72"
down_revision: Union[str, Sequence[str], None] = "e8b4c2a17d31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vocab_terms", sa.Column("owner_chat_id", sa.BigInteger(), nullable=True))
    op.create_index("ix_vocab_owner", "vocab_terms", ["owner_chat_id"])

    # `rank` is NOT NULL on the shared sheet. A learner's own word has no position in a
    # frequency list, so the column has to admit NULL — and NULLs sort first, which is where
    # somebody's own words belong in their list.
    with op.batch_alter_table("vocab_terms") as batch:
        batch.alter_column("rank", existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint("uq_vocab_term_it", type_="unique")

    op.create_index("uq_vocab_shared_it", "vocab_terms", ["it"], unique=True,
                    sqlite_where=sa.text("owner_chat_id IS NULL"))
    op.create_index("uq_vocab_own_it", "vocab_terms", ["owner_chat_id", "it"], unique=True,
                    sqlite_where=sa.text("owner_chat_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("uq_vocab_own_it", table_name="vocab_terms")
    op.drop_index("uq_vocab_shared_it", table_name="vocab_terms")
    op.drop_index("ix_vocab_owner", table_name="vocab_terms")
    # Own words go with the column; there is nowhere to keep them.
    op.execute("DELETE FROM vocab_terms WHERE owner_chat_id IS NOT NULL")
    with op.batch_alter_table("vocab_terms") as batch:
        batch.create_unique_constraint("uq_vocab_term_it", ["it"])
        batch.alter_column("rank", existing_type=sa.Integer(), nullable=False)
    op.drop_column("vocab_terms", "owner_chat_id")
