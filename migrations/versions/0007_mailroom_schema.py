"""Add the simulated mail provider's schema.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-29

The mailroom lives in its own Postgres schema and shares no table with the agent, like the
ledger: the two only meet through the MCP server. A contact is the only address a supplier can
be written to (the provider refuses any other recipient). Messages are append-only, out or in:
a conversation is a record, nothing in it is ever edited. A sent message carries the
idempotency key of the request that made it, so sending twice with the same key writes once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "mailroom"
TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")

    op.create_table(
        "contacts",
        sa.Column("supplier_siren", sa.String(9), primary_key=True),
        sa.Column("address", sa.String(254), nullable=False, unique=True),
        schema=SCHEMA,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("thread_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(3), nullable=False),
        sa.Column("supplier_siren", sa.String(9), nullable=False),
        sa.Column("address", sa.String(254), nullable=False),
        sa.Column("subject", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=True),
        sa.Column("iban_redacted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.ForeignKeyConstraint(["supplier_siren"], [f"{SCHEMA}.contacts.supplier_siren"]),
        sa.CheckConstraint("direction IN ('out', 'in')", name="direction_valid"),
        # Only what we send has a key: a reply is not the result of a request.
        sa.CheckConstraint(
            "(direction = 'out') = (idempotency_key IS NOT NULL)", name="out_has_key"
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL) = (request_hash IS NULL)", name="key_and_hash"
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_messages_thread", "messages", ["thread_id", "created_at"], schema=SCHEMA)
    op.create_index("ix_messages_inbound", "messages", ["direction", "created_at"], schema=SCHEMA)

    op.execute(
        f"CREATE TRIGGER trg_messages_append_only BEFORE UPDATE OR DELETE "
        f"ON {SCHEMA}.messages FOR EACH ROW EXECUTE FUNCTION public.forbid_modification()"
    )


def downgrade() -> None:
    op.execute(f"DROP SCHEMA {SCHEMA} CASCADE")
