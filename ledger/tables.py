"""Tables of the mock accounting system (Postgres schema ``ledger``).

They are declared separately from the agent's models on purpose: the ledger shares no table
with the agent. ``migrations/versions/0005_ledger_schema.py`` creates them, and a test
checks that the two agree.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

SCHEMA = "ledger"
metadata = MetaData(schema=SCHEMA)

accounts = Table(
    "accounts",
    metadata,
    Column("number", String(16), primary_key=True),
    Column("label", String(200), nullable=False),
)

suppliers = Table(
    "suppliers",
    metadata,
    Column("code", String(32), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("siren", String(9), nullable=False, unique=True),
    Column("siret", String(14), nullable=False),
    Column("vat_number", String(20), nullable=True),
    Column("iban_masked", String(40), nullable=True),
    Column("default_expense_account", String(16), nullable=False),
    Column("supplier_account", String(16), nullable=False),
)

purchase_orders = Table(
    "purchase_orders",
    metadata,
    Column("number", String(32), primary_key=True),
    Column("supplier_code", String(32), ForeignKey(f"{SCHEMA}.suppliers.code"), nullable=False),
    Column("order_date", Date, nullable=False),
    Column("status", String(16), nullable=False),
    Column("total_ht", Numeric(12, 2), nullable=False),
    CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="po_status"),
)

purchase_order_lines = Table(
    "purchase_order_lines",
    metadata,
    Column("po_number", String(32), nullable=False),
    Column("position", Integer, nullable=False),
    Column("description", String(500), nullable=False),
    Column("quantity", Numeric(12, 3), nullable=False),
    Column("unit_price", Numeric(12, 2), nullable=False),
    Column("amount_ht", Numeric(12, 2), nullable=False),
    PrimaryKeyConstraint("po_number", "position"),
    ForeignKeyConstraint(["po_number"], [f"{SCHEMA}.purchase_orders.number"]),
)

entries = Table(
    "entries",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("request_hash", String(64), nullable=False),
    Column("supplier_siren", String(9), nullable=False),
    Column("invoice_number", String(100), nullable=False),
    Column("journal", String(16), nullable=False),
    Column("entry_date", Date, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("supplier_siren", "invoice_number", name="uq_entries_invoice"),
)

entry_lines = Table(
    "entry_lines",
    metadata,
    Column("entry_id", Uuid, nullable=False),
    Column("position", Integer, nullable=False),
    Column("account", String(16), nullable=False),
    Column("label", String(300), nullable=False),
    Column("side", String(6), nullable=False),
    Column("amount", Numeric(12, 2), nullable=False),
    PrimaryKeyConstraint("entry_id", "position"),
    ForeignKeyConstraint(["entry_id"], [f"{SCHEMA}.entries.id"]),
    CheckConstraint("side IN ('debit', 'credit')", name="line_side"),
    CheckConstraint("amount > 0", name="line_amount_positive"),
)
