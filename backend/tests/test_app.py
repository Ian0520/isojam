from app.main import create_app

from fastapi.testclient import TestClient
import pytest

def test_app_model_session_lifecycle(
        fake_model_session,
        jwt_secret_key,
        ):
    def fake_factory():
        return fake_model_session
    test_app = create_app(
        model_session_factory=fake_factory,
        jwt_secret_key=jwt_secret_key,
        )

    with TestClient(test_app) as client:
        assert test_app.state.model_session is fake_model_session
        assert not fake_model_session.closed
        
        response = client.get("/health")
        assert response.status_code == 200

    assert fake_model_session.closed

def test_app_uses_injected_jwt_secret_key(
    fake_model_session,
    jwt_secret_key,
    monkeypatch,
):
    monkeypatch.delenv("ISOJAM_JWT_SECRET_KEY", raising=False)
    def fake_factory():
        return fake_model_session
    test_app = create_app(
        model_session_factory=fake_factory,
        jwt_secret_key=jwt_secret_key
    )
    with TestClient(test_app):
        assert test_app.state.jwt_secret_key == jwt_secret_key


def test_app_reads_jwt_secret_key_from_environment(
    fake_model_session,
    jwt_secret_key,
    monkeypatch,
):
    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", jwt_secret_key)
    def fake_factory():
        return fake_model_session
    test_app = create_app(model_session_factory=fake_factory)
    with TestClient(test_app):
        assert test_app.state.jwt_secret_key == jwt_secret_key

def test_app_rejects_missing_jwt_secret_before_loading_model(monkeypatch):
    monkeypatch.delenv("ISOJAM_JWT_SECRET_KEY", raising=False)
    def unexpected_model_factory():
        pytest.fail("Model must not load when JWT configuration is missing")
    test_app = create_app(model_session_factory=unexpected_model_factory)
    with pytest.raises(RuntimeError, match="ISOJAM_JWT_SECRET_KEY"):
        with TestClient(test_app):
            pass