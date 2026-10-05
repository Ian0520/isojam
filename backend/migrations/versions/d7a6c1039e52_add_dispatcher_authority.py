"""Add dispatcher ownership, generation, and reservation expiry.

Revision ID: d7a6c1039e52
Revises: c92fd8b7a104
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a6c1039e52"
down_revision = "c92fd8b7a104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old attempts retain their phase and capacity, without invented authority.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(sa.Column("dispatcher_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "dispatcher_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column("reservation_expires_at", sa.DateTime(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_job_attempts_dispatcher_authority",
            "(dispatcher_id IS NULL AND dispatcher_generation = 0 "
            "AND reservation_expires_at IS NULL) OR "
            "(dispatcher_id IS NOT NULL AND dispatcher_generation > 0 "
            "AND reservation_expires_at IS NOT NULL)",
        )


def downgrade() -> None:
    # Stop dispatchers before migrating. Downgrade deletes authority, not attempts.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("ck_job_attempts_dispatcher_authority", type_="check")
        batch_op.drop_column("reservation_expires_at")
        batch_op.drop_column("dispatcher_generation")
        batch_op.drop_column("dispatcher_id")
