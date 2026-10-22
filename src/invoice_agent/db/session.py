"""Engine and session handling.

Repositories never commit: the caller owns the transaction, so several repository calls
(for example "advance the invoice" and "append an audit event") succeed or fail together.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_CONNECT_TIMEOUT_S = 5
DEFAULT_STATEMENT_TIMEOUT_MS = 10_000


def database_url_from_env() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    return url


def make_engine(
    url: str,
    *,
    connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S,
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
) -> Engine:
    """Engine with explicit timeouts: a hung database must not hang the worker."""
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": connect_timeout_s,
            "options": f"-c statement_timeout={statement_timeout_ms}",
        },
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: domain objects are built from rows right away, nothing
    # lazily reloads after a commit.
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
