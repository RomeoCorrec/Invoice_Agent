"""The Postgres connection behind the graph's checkpoints."""

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from sqlalchemy.engine import make_url

CONNECT_TIMEOUT_S = 5


def checkpointer_dsn(database_url: str) -> str:
    """A libpq URL from the SQLAlchemy one (``postgresql+psycopg://`` becomes ``postgresql://``)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@contextmanager
def open_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """A saver on its own autocommit connection, with its tables created if missing.

    The checkpoint tables belong to LangGraph and are created by ``setup()``, not by Alembic.
    """
    with Connection.connect(
        checkpointer_dsn(database_url),
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        connect_timeout=CONNECT_TIMEOUT_S,
    ) as connection:
        saver = PostgresSaver(connection)
        saver.setup()
        yield saver
