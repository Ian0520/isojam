from datetime import datetime

from sqlalchemy import select, update
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


def _redemption_conditions(email: str, code_hash: str, now: datetime):
    return (
        RegistrationInvitation.email == email,
        RegistrationInvitation.code_hash == code_hash,
        RegistrationInvitation.used_at.is_(None),
        RegistrationInvitation.created_at <= now,
        RegistrationInvitation.expires_at > now,
    )


def is_available(
    session: Session, *, email: str, code_hash: str, now: datetime
) -> bool:
    """Preflight only: this read cannot grant permission to redeem."""
    return (
        session.scalar(
            select(RegistrationInvitation.id).where(
                *_redemption_conditions(email, code_hash, now)
            )
        )
        is not None
    )


def consume_invitation(
    session: Session, *, email: str, code_hash: str, now: datetime
) -> bool:
    """Conditionally claim once in the caller's write transaction; never commit."""
    statement = (
        update(RegistrationInvitation)
        .where(*_redemption_conditions(email, code_hash, now))
        .values(used_at=now)
        .returning(RegistrationInvitation.id)
        .execution_options(synchronize_session=False)
    )
    return session.scalar(statement) is not None
