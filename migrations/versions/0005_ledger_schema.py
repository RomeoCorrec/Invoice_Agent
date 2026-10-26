"""Add the mock accounting system's schema.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25

The ledger lives in its own Postgres schema and shares no table with the agent: the two only
meet through the MCP server. Posted entries are append-only (the trigger function is the one
created for the agent's own journal), a supplier can be booked once per invoice number, and a
posting key can be used once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "ledger"
TIMESTAMPTZ = sa.DateTime(timezone=True)
APPEND_ONLY_TABLES = ("entries", "entry_lines")


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")

    op.create_table(
        "accounts",
        sa.Column("number", sa.String(16), primary_key=True),
        sa.Column("label", sa.String(200), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "suppliers",
        sa.Column("code", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("siren", sa.String(9), nullable=False, unique=True),
        sa.Column("siret", sa.String(14), nullable=False),
        sa.Column("vat_number", sa.String(20), nullable=True),
        sa.Column("iban_masked", sa.String(40), nullable=True),
        sa.Column("default_expense_account", sa.String(16), nullable=False),
        sa.Column("supplier_account", sa.String(16), nullable=False),
        schema=SCHEMA,
    )
    op.create_table(
        "purchase_orders",
        sa.Column("number", sa.String(32), primary_key=True),
        sa.Column("supplier_code", sa.String(32), nullable=False),
        sa.Column("order_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("total_ht", sa.Numeric(12, 2), nullable=False),
        sa.ForeignKeyConstraint(["supplier_code"], [f"{SCHEMA}.suppliers.code"]),
        sa.CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="po_status"),
        schema=SCHEMA,
    )
    op.create_table(
        "purchase_order_lines",
        sa.Column("po_number", sa.String(32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("amount_ht", sa.Numeric(12, 2), nullable=False),
        sa.PrimaryKeyConstraint("po_number", "position"),
        sa.ForeignKeyConstraint(["po_number"], [f"{SCHEMA}.purchase_orders.number"]),
        schema=SCHEMA,
    )

    op.create_table(
        "entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("supplier_siren", sa.String(9), nullable=False),
        sa.Column("invoice_number", sa.String(100), nullable=False),
        sa.Column("journal", sa.String(16), nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.UniqueConstraint("supplier_siren", "invoice_number", name="uq_entries_invoice"),
        schema=SCHEMA,
    )
    op.create_table(
        "entry_lines",
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("account", sa.String(16), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("side", sa.String(6), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.PrimaryKeyConstraint("entry_id", "position"),
        sa.ForeignKeyConstraint(["entry_id"], [f"{SCHEMA}.entries.id"]),
        sa.CheckConstraint("side IN ('debit', 'credit')", name="line_side"),
        sa.CheckConstraint("amount > 0", name="line_amount_positive"),
        schema=SCHEMA,
    )

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE "
            f"ON {SCHEMA}.{table} FOR EACH ROW EXECUTE FUNCTION public.forbid_modification()"
        )


def downgrade() -> None:
    op.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
