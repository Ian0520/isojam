"""Add job timestamps, execution backend, and attempt history.

Revision ID: b6e89a52d107
Revises: 47d7085ef617
"""

import sqlalchemy as sa
from alembic import op

revision = "b6e89a52d107"
down_revision = "47d7085ef617"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite needs table recreation for CURRENT_TIMESTAMP column defaults.
    # Existing timestamps become migration time; historical times are unknown.
    with op.batch_alter_table("jobs", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "execution_backend", sa.String(), nullable=False, server_default="local"
            )
        )
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.current_timestamp(),
            )
        )
        batch_op.create_check_constraint(
            "ck_jobs_execution_backend", "execution_backend IN ('local', 'queued')"
        )

    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False, server_default="reserved"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.UniqueConstraint(
            "job_id", "attempt_number", name="uq_job_attempts_job_number"
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_job_attempts_positive_number"
        ),
        sa.CheckConstraint(
            "phase IN ('reserved', 'submitting', 'submitted', 'running', "
            "'result_ready', 'uncertain', 'succeeded', 'failed')",
            name="ck_job_attempts_phase",
        ),
    )


def downgrade() -> None:
    op.drop_table("job_attempts")
    with op.batch_alter_table("jobs", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_jobs_execution_backend", type_="check")
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("execution_backend")
