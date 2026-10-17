from datetime import datetime

from sqlalchemy.orm import Session

from app.db_models import RegistrationInvitation


def create_invitation(
    session: Session,
    *,
    email: str,
    code_hash: str,
    created_at: datetime,
    expires_at: datetime,
) -> RegistrationInvitation:
    """Insert into the caller's transaction; store no code and never commit."""
    invitation = RegistrationInvitation(
        email=email,
        code_hash=code_hash,
        created_at=created_at,
        expires_at=expires_at,
    )
    session.add(invitation)
    session.flush()
    return invitation
