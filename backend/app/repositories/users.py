from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import User


def create_user(
    session: Session,
    email: str,
    password_hash: str,
) -> User:
    user = User(
        email=email,
        password_hash=password_hash,
    )
    session.add(user)
    session.flush()
    return user


def get_user(session: Session, user_id: UUID) -> User | None:
    return session.get(User, user_id)


def get_user_by_email(session: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    user = session.scalars(statement).one_or_none()
    return user
