from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect

from invoice_agent.db.base import Base
from tests.integration.conftest import alembic_config

EXPECTED_TABLES = {
    "suppliers",
    "purchase_orders",
    "purchase_order_lines",
    "invoices",
    "invoice_lines",
    "invoice_vat_lines",
    "journal_entries",
    "journal_lines",
    "audit_events",
}


def test_upgrade_creates_every_table(engine: Engine) -> None:
    assert EXPECTED_TABLES.issubset(inspect(engine).get_table_names())


def test_models_and_migration_agree(engine: Engine) -> None:
    """The hand-written migration must not drift from the SQLAlchemy models."""
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_downgrade_then_upgrade_round_trip(engine: Engine, postgres_url: str) -> None:
    config = alembic_config(postgres_url)
    command.downgrade(config, "base")
    assert not EXPECTED_TABLES & set(inspect(engine).get_table_names())
    command.upgrade(config, "head")
    assert EXPECTED_TABLES.issubset(inspect(engine).get_table_names())
