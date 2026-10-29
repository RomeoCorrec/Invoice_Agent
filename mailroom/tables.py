"""Tables of the simulated mail provider (Postgres schema ``mailroom``).

Declared apart from the agent's models on purpose: the mailroom shares no table with the agent.
``migrations/versions/0007_mailroom_schema.py`` creates them, and a test checks that the two
agree.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    Text,
    Uuid,
    false,
)

SCHEMA = "mailroom"
metadata = MetaData(schema=SCHEMA)

contacts = Table(
    "contacts",
    metadata,
    Column("supplier_siren", String(9), primary_key=True),
    Column("address", String(254), nullable=False, unique=True),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("thread_id", Uuid, nullable=False),
    Column("direction", String(3), nullable=False),
    Column("supplier_siren", String(9), nullable=False),
    Column("address", String(254), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("idempotency_key", String(200), nullable=True, unique=True),
    Column("request_hash", String(64), nullable=True),
    Column("iban_redacted", Boolean, nullable=False, server_default=false()),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["supplier_siren"], [f"{SCHEMA}.contacts.supplier_siren"]),
    CheckConstraint("direction IN ('out', 'in')", name="direction_valid"),
    CheckConstraint("(direction = 'out') = (idempotency_key IS NOT NULL)", name="out_has_key"),
    CheckConstraint("(idempotency_key IS NULL) = (request_hash IS NULL)", name="key_and_hash"),
    Index("ix_messages_thread", "thread_id", "created_at"),
    Index("ix_messages_inbound", "direction", "created_at"),
)
