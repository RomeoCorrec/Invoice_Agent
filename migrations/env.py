"""Alembic environment.

The URL is taken, in order, from ``-x url=...``, the ``sqlalchemy.url`` option set
programmatically (used by the integration tests), then ``DATABASE_URL``.
"""

import os

from alembic import context
from sqlalchemy import create_engine

from invoice_agent.db import models  # noqa: F401  (registers the tables on Base.metadata)
from invoice_agent.db.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or config.get_main_option(
        "sqlalchemy.url"
    )
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL (or pass -x url=...) to run migrations")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
