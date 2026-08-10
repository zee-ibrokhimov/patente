"""translation language, chosen separately from the interface language

A learner reads the app in one language and may want the QUESTIONS in another. The two were
one field: `users.lang` picked the interface strings and, through `translations.deliver`,
the language a question was translated into. So an Uzbek speaker who reads Russian more
comfortably — common, and the reason Uzbek shipped as beta — had to switch the whole app to
Russian to read Russian translations.

NULL means "follow the interface language", which is what every existing row wants and what
the product did before this column existed. It is deliberately not defaulted to the current
`lang` value: copying it would freeze today's choice into a second place, and a learner who
later switched the app to English would keep getting Russian translations with nothing on
screen explaining why.

No server_default and nullable=True, so the existing rows need no backfill.

Revision ID: d7a3e1c95f04
Revises: c4d1f0a91b23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3e1c95f04"
down_revision: Union[str, Sequence[str], None] = "c4d1f0a91b23"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("translation_lang", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "translation_lang")
