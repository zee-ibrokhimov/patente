"""Alembic environment.

The database URL comes from shared.config (and therefore .env), never from
alembic.ini — one source of truth, and no credentials in a committed file.
Migrations run against the sync driver even though the API is async.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from api.models import Base  # noqa: E402
from shared.db import make_sync_engine, sync_url  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rebuilds the table
        # instead of failing. Needed for anything beyond an additive migration.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = make_sync_engine(poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
