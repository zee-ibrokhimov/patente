"""the shared translation memory for words tapped inside a question

NOT the glossary. `vocab_terms` with a NULL owner is a curated, frequency-ranked sheet of
1,104 exam words that the drill walks in teaching order; writing every tapped word into it
would destroy both the curation and the ordering. The drill never reads this table.

WHY IT IS SHARED WHILE SAVED WORDS ARE PERSONAL

Measured against the real bank before this was built: the glossary covers 14.5% of the word
tokens in the questions, and the words a learner is likeliest to tap are the ones missing —
`raffigurato` occurs 2,796 times and is absent, as are `veicolo`, `veicoli` and `velocità`.
So almost every tap would reach the model.

There are only 5,239 distinct words in the whole bank. Shared, the first learner to tap a
word pays for it and everyone after gets it instantly, so the ceiling is the bank translated
once rather than a cost that grows with users.

KEYED ON THE DICTIONARY FORM, because `veicolo` and `veicoli` are two tokens and one word.

Revision ID: e2b8f47a1c53
Revises: d4a71c39e082
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2b8f47a1c53"
down_revision: Union[str, Sequence[str], None] = "d4a71c39e082"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "word_glosses",
        # The lemma IS the key: every read asks "do we already know this word", and a
        # surrogate id would need a unique index over this column anyway.
        sa.Column("lemma", sa.Text(), nullable=False),
        sa.Column("en", sa.Text(), nullable=False),
        sa.Column("ru", sa.Text(), nullable=False),
        sa.Column("uz", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("lemma"),
    )


    # THE CACHE IS USELESS WITHOUT THIS TABLE, and only a test noticed. Glosses are keyed on
    # the lemma (`raffigurare`) but a learner taps a surface form (`raffigurato`), so looking
    # the cache up by what was tapped missed on every inflected word — the second learner to
    # tap it paid again, and the third. The text returned was identical and correct; only the
    # bill was wrong, which is invisible until somebody reads an invoice.
    #
    # One row per form, so a verb with six inflections costs one model call and five cheap
    # inserts.
    op.create_table(
        "word_forms",
        sa.Column("form", sa.Text(), nullable=False),
        sa.Column("lemma", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("form"),
        sa.ForeignKeyConstraint(["lemma"], ["word_glosses.lemma"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("word_forms")
    op.drop_table("word_glosses")
