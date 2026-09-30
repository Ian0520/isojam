from sqlalchemy import select

from app.db_models import User


def create_user(
    session,
    email,
    password_hash,
):
    user = User(
        email=email,
        password_hash=password_hash,
    )
    session.add(user)
    session.flush()
    return user


def get_user(session, user_id):
    return session.get(User, user_id)


def get_user_by_email(session, email):
    statement = select(User).where(User.email == email)
    user = session.scalars(statement).one_or_none()
    return user
