from app.db_models import User
from sqlalchemy import select

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