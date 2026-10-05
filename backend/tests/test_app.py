import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.main import create_app
from app.repositories import users


def test_app_model_session_lifecycle(
    fake_model_session,
    jwt_secret_key,
):
    def fake_factory():
        return fake_model_session

    test_app = create_app(
        model_session_factory=fake_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
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
        model_session_factory=fake_factory, jwt_secret_key=jwt_secret_key
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


def test_app_uses_injected_db_session_factory_for_requests(
    fake_model_session,
    test_session_factory,
    jwt_secret_key,
    monkeypatch,
):
    def unexpected_global_session_factory():
        raise AssertionError("Requests must use the injected database session factory")

    monkeypatch.setattr(database, "SessionLocal", unexpected_global_session_factory)

    def fake_factory():
        return fake_model_session

    test_app = create_app(
        model_session_factory=fake_factory,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
    )
    email = "injected-db@example.com"

    with TestClient(test_app) as client:
        response = client.post(
            "/register",
            json={"email": email, "password": "correct-horse-battery-staple"},
        )
        assert response.status_code == 201

    with test_session_factory() as session:
        user = users.get_user_by_email(session, email)
        assert user is not None
        assert str(user.id) == response.json()["id"]


def test_app_uses_injected_upload_limit(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", "invalid")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
        max_upload_bytes=1024,
    )
    with TestClient(test_app):
        assert test_app.state.max_upload_bytes == 1024


def test_app_reads_upload_limit_from_environment(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", "2048")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
    )
    with TestClient(test_app):
        assert test_app.state.max_upload_bytes == 2048


def test_app_rejects_invalid_upload_limit_before_loading_model(
    jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", "0")

    def unexpected_model_factory():
        pytest.fail("Model must not load when upload configuration is invalid")

    test_app = create_app(
        model_session_factory=unexpected_model_factory,
        jwt_secret_key=jwt_secret_key,
    )
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_UPLOAD_BYTES"):
        with TestClient(test_app):
            pass


@pytest.mark.parametrize("max_upload_bytes", [0, -1, True, 1.5])
def test_app_rejects_invalid_injected_upload_limit(max_upload_bytes):
    with pytest.raises(ValueError, match="max_upload_bytes"):
        create_app(max_upload_bytes=max_upload_bytes)


def test_app_uses_injected_audio_duration_limit(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "invalid")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
        max_audio_duration_seconds=30,
    )
    with TestClient(test_app):
        assert test_app.state.max_audio_duration_seconds == 30


def test_app_reads_audio_duration_limit_from_environment(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "120")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
    )
    with TestClient(test_app):
        assert test_app.state.max_audio_duration_seconds == 120


def test_app_rejects_invalid_audio_duration_before_loading_model(
    jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "0")

    def unexpected_model_factory():
        pytest.fail("Model must not load when audio duration configuration is invalid")

    test_app = create_app(
        model_session_factory=unexpected_model_factory,
        jwt_secret_key=jwt_secret_key,
    )
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_AUDIO_DURATION_SECONDS"):
        with TestClient(test_app):
            pass


@pytest.mark.parametrize("max_audio_duration_seconds", [0, -1, True, 1.5])
def test_app_rejects_invalid_injected_audio_duration_limit(max_audio_duration_seconds):
    with pytest.raises(ValueError, match="max_audio_duration_seconds"):
        create_app(max_audio_duration_seconds=max_audio_duration_seconds)


def test_app_uses_injected_unfinished_job_limit(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", "invalid")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
        max_unfinished_jobs_per_user=3,
    )
    with TestClient(test_app):
        assert test_app.state.max_unfinished_jobs_per_user == 3


def test_app_reads_unfinished_job_limit_from_environment(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", "4")
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
    )
    with TestClient(test_app):
        assert test_app.state.max_unfinished_jobs_per_user == 4


def test_app_rejects_invalid_unfinished_job_limit_before_loading_model(
    jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", "0")

    def unexpected_model_factory():
        pytest.fail("Model must not load when unfinished job limit is invalid")

    test_app = create_app(
        model_session_factory=unexpected_model_factory, jwt_secret_key=jwt_secret_key
    )
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_UNFINISHED_JOBS_PER_USER"):
        with TestClient(test_app):
            pass


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_app_rejects_invalid_injected_unfinished_job_limit(limit):
    with pytest.raises(ValueError, match="max_unfinished_jobs_per_user"):
        create_app(max_unfinished_jobs_per_user=limit)
