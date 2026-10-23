"""Give audit events an insertion sequence.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23

Events written in the same step can share a timestamp and ids are random, so the order of
the trail was ambiguous. ``seq`` is the insertion order; reads sort by (occurred_at, seq).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "audit_events",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
    )
    op.create_unique_constraint(op.f("uq_audit_events_seq"), "audit_events", ["seq"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_audit_events_seq"), "audit_events", type_="unique")
    op.drop_column("audit_events", "seq")
