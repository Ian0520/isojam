from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.routers.auth as auth_routes
from app.config import get_registration_mode
from app.db_models import User
from app.main import create_app
from app.security import decode_access_token
from tests.factories import create_test_user


def test_registration_mode_defaults_to_closed(monkeypatch):
    monkeypatch.delenv("ISOJAM_REGISTRATION_MODE", raising=False)
    assert get_registration_mode() == "closed"


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_registration_mode_reads_environment(monkeypatch, mode):
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", mode)
    assert get_registration_mode() == mode


@pytest.mark.parametrize("mode", ["", " ", "OPEN", "invite", " open "])
def test_registration_mode_rejects_invalid_environment(monkeypatch, mode):
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", mode)
    with pytest.raises(RuntimeError, match="ISOJAM_REGISTRATION_MODE"):
        get_registration_mode()


@pytest.mark.parametrize("mode", ["", "OPEN", "invite", False, 1, []])
def test_invalid_injected_registration_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="registration_mode"):
        create_app(registration_mode=mode)


def test_invalid_registration_environment_fails_before_model_loading(
    jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", "invalid")
    model_factory = Mock(side_effect=AssertionError("Model must not load"))
    app = create_app(
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
        model_session_factory=model_factory,
    )
    with pytest.raises(RuntimeError, match="ISOJAM_REGISTRATION_MODE"):
        with TestClient(app):
            pass
    model_factory.assert_not_called()


@pytest.mark.parametrize("mode, expected_status", [("open", 201), ("closed", 403)])
def test_injected_registration_policy_overrides_environment(
    test_session_factory, jwt_secret_key, monkeypatch, mode, expected_status
):
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", "invalid")
    app = create_app(
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="disabled",
        registration_mode=mode,
    )
    with TestClient(app) as client:
        response = client.post(
            "/register", json={"email": "user@example.com", "password": "test-password"}
        )
    assert response.status_code == expected_status
    with test_session_factory() as session:
        assert len(session.scalars(select(User)).all()) == (1 if mode == "open" else 0)


@pytest.mark.parametrize("processing_mode", ["local", "disabled", "queued"])
def test_default_closed_registration_skips_account_lookup_hashing_and_creation(
    test_session_factory,
    jwt_secret_key,
    fake_model_session,
    monkeypatch,
    processing_mode,
):
    monkeypatch.delenv("ISOJAM_REGISTRATION_MODE", raising=False)
    with test_session_factory() as session:
        existing = create_test_user(session, email="existing@example.com")
        existing_id = existing.id
        session.commit()
    lookup, password_hash, create = Mock(), Mock(), Mock()
    monkeypatch.setattr(auth_routes.users, "get_user_by_email", lookup)
    monkeypatch.setattr(auth_routes, "hash_password", password_hash)
    monkeypatch.setattr(auth_routes.users, "create_user", create)
    app = create_app(
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode=processing_mode,
        model_session_factory=lambda: fake_model_session,
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for email in ("Existing@Example.COM", "unknown@example.com"):
            response = client.post(
                "/register", json={"email": email, "password": "test-password"}
            )
            assert response.status_code == 403
            assert response.json() == {"detail": "Registration is closed"}
    lookup.assert_not_called()
    password_hash.assert_not_called()
    create.assert_not_called()
    with test_session_factory() as session:
        assert session.scalars(select(User.id)).all() == [existing_id]


def test_registration_policy_is_loaded_on_startup_and_changes_on_restart(
    test_session_factory, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", "open")
    app = create_app(
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="disabled",
    )
    credentials = {"email": "user@example.com", "password": "test-password"}
    with TestClient(app) as client:
        monkeypatch.setenv("ISOJAM_REGISTRATION_MODE", "closed")
        created = client.post("/register", json=credentials)
        assert created.status_code == 201
        user_id = created.json()["id"]
    with TestClient(app) as client:
        for email in (credentials["email"], "unknown@example.com"):
            response = client.post("/register", json={**credentials, "email": email})
            assert response.status_code == 403
            assert response.json() == {"detail": "Registration is closed"}
        login = client.post("/login", json=credentials)
        assert login.status_code == 200
        assert (
            str(decode_access_token(login.json()["access_token"], jwt_secret_key))
            == user_id
        )
        assert (
            client.post("/login", json={**credentials, "password": "wrong"}).status_code
            == 401
        )
    with test_session_factory() as session:
        assert len(session.scalars(select(User)).all()) == 1
