"""Add guarded worker heartbeat metadata.

Revision ID: f3b8d2106a74
Revises: e4f21c8a906b
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b8d2106a74"
down_revision = "e4f21c8a906b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL means no heartbeat has been recorded; never invent past contact.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(
            sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_job_attempts_heartbeat_authority",
            "last_heartbeat_at IS NULL OR "
            "(invocation_id IS NOT NULL AND started_at IS NOT NULL "
            "AND last_heartbeat_at >= started_at)",
        )


def downgrade() -> None:
    # Stop control/worker processes first; downgrade only removes contact history.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("ck_job_attempts_heartbeat_authority", type_="check")
        batch_op.drop_column("last_heartbeat_at")
