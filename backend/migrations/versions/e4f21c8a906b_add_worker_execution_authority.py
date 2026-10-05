"""Add worker invocation identity and execution authorization deadline.

Revision ID: e4f21c8a906b
Revises: d7a6c1039e52
"""

import sqlalchemy as sa
from alembic import op

revision = "e4f21c8a906b"
down_revision = "d7a6c1039e52"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve historical attempts without inventing a deadline or invocation.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(
            sa.Column(
                "execution_authorization_expires_at", sa.DateTime(), nullable=True
            )
        )
        batch_op.add_column(sa.Column("invocation_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_job_attempts_invocation", ["invocation_id"]
        )
        batch_op.create_check_constraint(
            "ck_job_attempts_execution_authority",
            "(execution_authorization_expires_at IS NULL OR "
            "(dispatcher_id IS NOT NULL AND dispatcher_generation > 0)) AND "
            "(invocation_id IS NULL OR "
            "(execution_authorization_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND phase IN ('running', 'result_ready', 'uncertain', 'succeeded', 'failed')))",
        )


def downgrade() -> None:
    # Stop control/worker processes first. Dropping evidence cannot stop a worker.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("ck_job_attempts_execution_authority", type_="check")
        batch_op.drop_constraint("uq_job_attempts_invocation", type_="unique")
        batch_op.drop_column("invocation_id")
        batch_op.drop_column("execution_authorization_expires_at")
