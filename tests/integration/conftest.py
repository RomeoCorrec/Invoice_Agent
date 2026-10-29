"""Real PostgreSQL for integration tests (one container per session, via testcontainers).

The schema is created by running the Alembic migrations, so they are tested too.
"""

import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres import PostgresSaver
from mcp.server.mcpserver import MCPServer
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from invoice_agent.agent.graph.checkpoint import open_checkpointer
from invoice_agent.agent.graph.run import Graph
from invoice_agent.db.session import make_session_factory, session_scope
from ledger.seed import seed_reference
from ledger.server import build_server
from ledger.store import LedgerStore
from tests.integration.reference import load_reference
from tests.integration.support import graph_for
from tests.pdfs import INVOICES

ROOT = Path(__file__).parents[2]


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer("postgres:16", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def engine(postgres_url: str) -> Iterator[Engine]:
    command.upgrade(alembic_config(postgres_url), "head")
    engine = create_engine(postgres_url)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def _empty_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    """Start every test from empty tables (TRUNCATE is not blocked by append-only triggers)."""
    yield
    if "engine" not in request.fixturenames:
        return
    engine: Engine = request.getfixturevalue("engine")
    with engine.begin() as connection:
        tables = connection.execute(
            text(
                "SELECT string_agg(quote_ident(schemaname) || '.' || quote_ident(tablename), ', ') "
                "FROM pg_tables WHERE schemaname IN ('public', 'ledger', 'mailroom') "
                "AND tablename <> 'alembic_version'"
            )
        ).scalar_one()
        connection.execute(text(f"TRUNCATE {tables} CASCADE"))


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose uncommitted work is rolled back at the end of the test."""
    with Session(engine) as session:
        yield session
        session.rollback()


@pytest.fixture(scope="session")
def saver(postgres_url: str) -> Iterator[PostgresSaver]:
    """LangGraph's checkpoint store on the test database (its tables are emptied with the rest
    after each test, and created once here)."""
    with open_checkpointer(postgres_url) as saver:
        yield saver


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Sessions on the test database, with the dataset's suppliers and purchase orders."""
    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        load_reference(session)
    yield factory


@pytest.fixture
def documents(tmp_path: Path) -> Path:
    """A document store holding INV-01 (clean), INV-09 (plain PDF) and INV-12 (anomaly)."""
    root = tmp_path / "documents"
    root.mkdir()
    for case in ("INV-01", "INV-09", "INV-12"):
        [source] = INVOICES.glob(f"{case}_*.pdf")
        shutil.copy(source, root / source.name)
    return root


@pytest.fixture
def ledger_server(engine: Engine) -> MCPServer:
    """The mock accounting system, loaded with the reference data, served in memory."""
    seed_reference(engine)
    return build_server(LedgerStore(engine))


@pytest.fixture
def graph_factory(
    factory: sessionmaker[Session],
    documents: Path,
    saver: PostgresSaver,
    ledger_server: MCPServer,
) -> Callable[[], Graph]:
    """Builds the graph on demand, so a test can patch a node before the graph exists."""
    return lambda: graph_for(factory, documents, saver, ledger=ledger_server)


@pytest.fixture
def graph(graph_factory: Callable[[], Graph]) -> Graph:
    return graph_factory()
