"""Add free-text notes to invoices.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23

Notes are read by the prompt-injection check, which is why they are stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoices",
        sa.Column(
            "notes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("invoices", "notes")
