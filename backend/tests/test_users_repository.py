from app.db_models import User
import app.repositories.users as users

from uuid import UUID, uuid4
import pytest
from sqlalchemy.exc import IntegrityError

from tests.factories import create_test_user

def test_create_user_persists_after_commit(test_session_factory):
    with test_session_factory() as session:
        user = users.create_user(session, "user@example.com", "some-hash")
        assert user.id is not None
        user_id = user.id
        session.commit()

    with test_session_factory() as session:
        result = session.get(User, user_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.id == user_id
        assert result.email == "user@example.com"
        assert result.password_hash == "some-hash"

def test_get_user_returns_existing_user(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        assert user.id is not None
        user_id = user.id
        session.commit()

    with test_session_factory() as session:
        result = users.get_user(session, user_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.id == user_id
        assert result.email == "user@example.com"
        assert result.password_hash == "some-hash"

def test_get_user_returns_none_for_missing_user(test_session_factory):
    with test_session_factory() as session:
        result = users.get_user(session, uuid4())
        assert result is None

def test_get_user_by_email_returns_existing_user(test_session_factory):
    with test_session_factory() as session:
        email = "user@example.com"
        user = create_test_user(session, email=email)
        assert user is not None
        user_id = user.id
        session.commit()

    with test_session_factory() as session:
        result = users.get_user_by_email(session, email)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.id == user_id
        assert result.email == email
        assert result.password_hash == "some-hash"
        
def test_get_user_by_email_returns_none_for_missing_email(test_session_factory):
    with test_session_factory() as session:
        result = users.get_user_by_email(session, "fake@email.com")
        assert result is None

def test_create_user_rejects_duplicate_email(test_session_factory):
    with test_session_factory() as session:  
        email = "user@example.com"
        users.create_user(session, email, "some-hash")
        session.commit()
        with pytest.raises(IntegrityError):
            users.create_user(session, email, "another-hash")
        

