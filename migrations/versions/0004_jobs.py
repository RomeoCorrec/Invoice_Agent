"""Add the jobs queue.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-24

A job is polled by workers with FOR UPDATE SKIP LOCKED. A lease (locked_by/locked_until)
exists exactly while the job runs, so a crashed worker's job can be taken over once the
lease has expired.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", TIMESTAMPTZ, nullable=False),
        sa.Column("locked_by", sa.String(128)),
        sa.Column("locked_until", TIMESTAMPTZ),
        sa.Column("last_error", sa.Text()),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("created_at", TIMESTAMPTZ, nullable=False),
        sa.Column("updated_at", TIMESTAMPTZ, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_jobs_idempotency_key")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'dead')",
            name=op.f("ck_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "(status = 'running') = (locked_by IS NOT NULL)", name=op.f("ck_jobs_lease_worker")
        ),
        sa.CheckConstraint(
            "(status = 'running') = (locked_until IS NOT NULL)", name=op.f("ck_jobs_lease_expiry")
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name=op.f("ck_jobs_attempts_bounded")
        ),
        sa.CheckConstraint("max_attempts >= 1", name=op.f("ck_jobs_max_attempts_positive")),
    )
    op.create_index("ix_jobs_status_run_after", "jobs", ["status", "run_after"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_run_after", table_name="jobs")
    op.drop_table("jobs")
