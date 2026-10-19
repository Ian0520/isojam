"""Persist the bounded YouTube import queue.

Revision ID: d9a1e6240f83
Revises: c8f2a6139d40
"""

import sqlalchemy as sa
from alembic import op

revision = "d9a1e6240f83"
down_revision = "c8f2a6139d40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audio_imports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("video_id", sa.String(11), nullable=False),
        sa.Column("submission_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("upload_id", sa.Uuid(), sa.ForeignKey("uploads.id"), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "user_id", "submission_key", name="uq_audio_imports_submission"
        ),
        sa.CheckConstraint(
            "length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'",
            name="ck_audio_imports_video_id",
        ),
        sa.CheckConstraint(
            "length(submission_key) BETWEEN 1 AND 128",
            name="ck_audio_imports_submission_key",
        ),
        sa.CheckConstraint(
            "(status IN ('pending', 'processing') AND upload_id IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'ready' AND upload_id IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND upload_id IS NULL AND error_code IS NOT NULL)",
            name="ck_audio_imports_result",
        ),
    )
    op.create_index(
        "ix_audio_imports_queue", "audio_imports", ["status", "created_at", "id"]
    )
    op.create_index(
        "ix_audio_imports_owner", "audio_imports", ["user_id", "created_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("audio_imports")
