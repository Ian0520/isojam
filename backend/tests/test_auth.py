from app.db_models import User
from app.security import verify_password, decode_access_token

from uuid import UUID
from sqlalchemy import select


def test_register_creates_user(client, test_session_factory):
    email = "user@example.com"
    password = "correct-horse-battery-staple"
    response = client.post(
        "/register",
        json={
            "email": email,
            "password": password,
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert "id" in data
    user_id = UUID(data["id"])
    assert data["email"] == email

    assert "password" not in data
    assert "password_hash" not in data

    with test_session_factory() as session:
        statement = select(User).where(User.email == email)
        user = session.scalars(statement).one_or_none()
        assert user is not None
        assert user.id == user_id
        assert user.email == email
        assert user.password_hash != password
        assert verify_password(password, user.password_hash)
    
def test_register_normalizes_email(client, test_session_factory):
    submitted_email = "User@Example.COM"
    expected_email = "user@example.com"
    password = "correct-horse-battery-staple"
    response = client.post(
        "/register",
        json={
            "email": submitted_email,
            "password": password,
        },
    )
    assert response.status_code == 201
    assert response.json()["email"] == expected_email

    with test_session_factory() as session:
        statement = select(User).where(User.email == expected_email)
        user = session.scalars(statement).one_or_none()
        assert user is not None
        assert user.email == expected_email

def test_register_rejects_duplicate_email(client):
    response = client.post(
        "/register",
        json={
            "email": "User@Example.com",
            "password": "correct-horse-battery-staple",
        },
    )
    assert response.status_code == 201

    dup_response = client.post(
        "/register",
        json={
            "email": "user@example.COM",
            "password": "another-password",
        },
    )
    assert dup_response.status_code == 409
    assert dup_response.json()["detail"] == "Email is already registered"






def test_login_returns_access_token(client, jwt_secret_key):
    email = "user@example.com"
    password = "correct-horse-battery-staple"
    register_response = client.post(
        "/register",
        json={
            "email": email,
            "password": password,
        },
    )
    assert register_response.status_code == 201
    registered_user_id = UUID(register_response.json()["id"])

    login_response = client.post(
        "/login",
        json={
            "email": email,
            "password": password,
        },
    )
    assert login_response.status_code == 200

    data = login_response.json()
    access_token = data["access_token"]

    assert isinstance(access_token, str)
    assert access_token
    assert data["token_type"] == "bearer"

    decoded_user_id = decode_access_token(
        access_token,
        jwt_secret_key,
    )

    assert decoded_user_id == registered_user_id

def test_login_normalizes_email(client):
    normalized_email = "user@example.com"
    submitted_email = "User@Example.COM"
    password = "correct-horse-battery-staple"

    register_response = client.post(
        "/register",
        json={
            "email": normalized_email,
            "password": password,
        },
    )
    assert register_response.status_code == 201

    login_response = client.post(
        "/login",
        json={
            "email": submitted_email,
            "password": password,
        },
    )

    assert login_response.status_code == 200
    data = login_response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_rejects_incorrect_password(client):
    email = "user@example.com"
    password = "correct-horse-battery-staple"
    register_response = client.post(
        "/register",
        json={
            "email": email,
            "password": password,
        },
    )

    assert register_response.status_code == 201

    login_response = client.post(
        "/login",
        json={
            "email": email,
            "password": "incorrect-password"
        }
    )
    assert login_response.status_code == 401
    assert login_response.json()["detail"] == "Invalid email or password"

def test_login_rejects_unknown_email(client):
    email = "user@example.com"
    password = "correct-horse-battery-staple"
    login_response = client.post(
        "/login",
        json={
            "email": email,
            "password": password,
        }
    )
    assert login_response.status_code == 401
    assert login_response.json()["detail"] == "Invalid email or password"