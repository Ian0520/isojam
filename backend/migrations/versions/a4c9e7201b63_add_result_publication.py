"""Record the selected manifest for guarded result publication.

Revision ID: a4c9e7201b63
Revises: f3b8d2106a74
"""

import sqlalchemy as sa
from alembic import op

revision = "a4c9e7201b63"
down_revision = "f3b8d2106a74"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing attempts stay NULL: migration cannot invent published evidence.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.add_column(
            sa.Column("result_manifest_key", sa.String(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("result_manifest_sha256", sa.String(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_job_attempts_result_publication",
            "(result_manifest_key IS NULL AND result_manifest_sha256 IS NULL) OR "
            "(result_manifest_key IS NOT NULL AND result_manifest_sha256 IS NOT NULL "
            "AND length(result_manifest_key) BETWEEN 1 AND 256 "
            "AND length(result_manifest_sha256) = 64 "
            "AND result_manifest_sha256 NOT GLOB '*[^0-9a-f]*' "
            "AND invocation_id IS NOT NULL AND phase = 'succeeded' "
            "AND finished_at IS NOT NULL AND finished_at >= started_at)",
        )


def downgrade() -> None:
    # Stop control processes first. Output rows/files remain; selected-manifest
    # history is lost, so old publication acknowledgements cannot be reconstructed.
    with op.batch_alter_table("job_attempts") as batch_op:
        batch_op.drop_constraint("ck_job_attempts_result_publication", type_="check")
        batch_op.drop_column("result_manifest_sha256")
        batch_op.drop_column("result_manifest_key")
