from app.db_models import User
from app.security import verify_password, decode_access_token
from app.repositories import users
from app.main import create_app
from app.database import get_db
from app.auth import get_current_user
from tests.factories import create_test_user
from app.security import create_access_token

from datetime import timedelta
from uuid import UUID, uuid4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient
from fastapi import Depends

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


def test_register_handles_duplicate_email_race(client, monkeypatch):
    def simulate_duplicate_email(session, email, password_hash):
        raise IntegrityError(
            statement="INSERT INTO users ...",
            params=None,
            orig=Exception("Duplicate email"),
        )
    monkeypatch.setattr(users, "create_user", simulate_duplicate_email)

    email = "user@example.com"
    password = "correct-horse-battery-staple"
    response = client.post("/register"
                    , json={
                        "email": email,
                        "password": password
                    })
    assert response.status_code == 409
    assert response.json()["detail"] == "Email is already registered"

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

def test_login_uses_jwt_secret_key_from_environment(
    fake_model_session,
    test_session_factory,
    jwt_secret_key,
    monkeypatch,
):
    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", jwt_secret_key)
    def override_get_db():
        with test_session_factory() as session:
            yield session

    def fake_factory():
        return fake_model_session

    test_app = create_app(
        model_session_factory=fake_factory,
        db_session_factory=test_session_factory,
    )

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app) as client:
        email = "user@example.com"
        password = "correct-horse-battery-staple"
        register_response = client.post(
            "/register",
            json={
                "email": email,
                "password": password,
            }
        )
        assert register_response.status_code == 201
        user_id = UUID(register_response.json()["id"])

        login_response = client.post(
            "/login",
            json={
                "email": email,
                "password": password,
            }
        )
        assert login_response.status_code == 200
        access_token = login_response.json()["access_token"]

        decoded_user_id = decode_access_token(access_token, jwt_secret_key)
        assert user_id == decoded_user_id

def test_get_current_user_rejects_missing_token(client):
    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}
    
    response = client.get("/test/current-user")
    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_get_current_user_returns_authenticated_user(
    client,
    test_session_factory,
    jwt_secret_key,
):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )

    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}

    response = client.get(
    "/test/current-user",
    headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == str(user_id)

def test_get_current_user_rejects_invalid_token(client):
    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}
    
    response = client.get(
        "/test/current-user",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_get_current_user_rejects_expired_token(
    client,
    test_session_factory,
    jwt_secret_key,
):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=-1),
    )

    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}

    response = client.get(
    "/test/current-user",
    headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_get_current_user_rejects_wrong_signing_key(
    client,
    test_session_factory,
):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id,
        secret_key="different-test-signing-secret-at-least-32-bytes",
        expires_delta=timedelta(minutes=5),
    )

    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}

    response = client.get(
    "/test/current-user",
    headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"

def test_get_current_user_rejects_nonexistent_user(client, jwt_secret_key):
    user_id = uuid4()
    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )

    @client.app.get("/test/current-user")
    def current_user_endpoint(current_user=Depends(get_current_user)):
        return {"id": str(current_user.id)}

    response = client.get(
    "/test/current-user",
    headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"