"""Persist controller-confirmed execution stop evidence.

Revision ID: b5d8f3027a91
Revises: a4c9e7201b63
"""

import sqlalchemy as sa
from alembic import op

revision = "b5d8f3027a91"
down_revision = "a4c9e7201b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old timestamps/manifests cannot establish that a process actually stopped.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(sa.Column("local_worker_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("local_worker_pid", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_job_attempts_local_worker",
            "(local_worker_pid IS NULL AND local_worker_id IS NULL) OR "
            "(local_worker_pid IS NOT NULL AND local_worker_id IS NOT NULL "
            "AND typeof(local_worker_pid) = 'integer' AND local_worker_pid > 0 "
            "AND invocation_id IS NOT NULL AND started_at IS NOT NULL)",
        )
        batch_op.add_column(
            sa.Column("execution_stopped_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("execution_exit_code", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_job_attempts_execution_stopped",
            "(execution_stopped_at IS NULL AND execution_exit_code IS NULL) OR "
            "(execution_stopped_at IS NOT NULL AND execution_exit_code IS NOT NULL "
            "AND typeof(execution_exit_code) = 'integer' "
            "AND execution_exit_code BETWEEN -255 AND 255 "
            "AND invocation_id IS NOT NULL AND started_at IS NOT NULL "
            "AND execution_stopped_at >= started_at "
            "AND (last_heartbeat_at IS NULL OR execution_stopped_at >= last_heartbeat_at) "
            "AND (finished_at IS NULL OR finished_at >= execution_stopped_at) "
            "AND phase IN ('running', 'result_ready', 'uncertain', 'succeeded', 'failed'))",
        )


def downgrade() -> None:
    # Stop controllers first. Outputs and publication history survive, but lost
    # stop evidence cannot be reconstructed on re-upgrade.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("ck_job_attempts_execution_stopped", type_="check")
        batch_op.drop_constraint("ck_job_attempts_local_worker", type_="check")
        batch_op.drop_column("local_worker_pid")
        batch_op.drop_column("local_worker_id")
        batch_op.drop_column("execution_exit_code")
        batch_op.drop_column("execution_stopped_at")
