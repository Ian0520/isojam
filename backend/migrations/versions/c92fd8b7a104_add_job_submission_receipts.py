"""Add durable, per-user job submission receipts.

Revision ID: c92fd8b7a104
Revises: b6e89a52d107
"""

import sqlalchemy as sa
from alembic import op

revision = "c92fd8b7a104"
down_revision = "b6e89a52d107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_submission_receipts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint("user_id", "key"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.CheckConstraint(
            "length(key) BETWEEN 1 AND 128", name="ck_job_submission_key_length"
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_job_submission_fingerprint_length",
        ),
    )


def downgrade() -> None:
    # Keep jobs and their results; only remove the ability to replay old keys.
    op.drop_table("job_submission_receipts")
