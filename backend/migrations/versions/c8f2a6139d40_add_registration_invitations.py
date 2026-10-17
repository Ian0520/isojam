"""Store email-bound registration invitations without plaintext codes.

Revision ID: c8f2a6139d40
Revises: b5d8f3027a91
"""

import sqlalchemy as sa
from alembic import op

revision = "c8f2a6139d40"
down_revision = "b5d8f3027a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash", name="uq_registration_invitations_code_hash"),
        sa.CheckConstraint(
            "length(email) BETWEEN 3 AND 254 AND email = lower(trim(email))",
            name="ck_registration_invitations_email",
        ),
        sa.CheckConstraint(
            "length(code_hash) = 64 AND code_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_registration_invitations_code_hash",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_registration_invitations_expiry"
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR (used_at >= created_at AND used_at < expires_at)",
            name="ck_registration_invitations_used_at",
        ),
    )


def downgrade() -> None:
    # Existing accounts, uploads and jobs remain; invitation history is removed.
    op.drop_table("registration_invitations")
