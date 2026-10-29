"""Add the supplier's contact address.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-29

The supplier correspondence (ADR-0014) writes to a supplier. The address comes from this column
of the referential and nowhere else: not from an invoice, not from a model, so a document cannot
choose where a message goes. It is optional: a supplier without one cannot be written to.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("suppliers", sa.Column("contact_email", sa.String(254), nullable=True))


def downgrade() -> None:
    op.drop_column("suppliers", "contact_email")
