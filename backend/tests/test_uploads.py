from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.repositories.uploads as uploads
import app.storage as storage
from app.db_models import Upload
from app.main import create_app
from app.security import create_access_token
from tests.factories import create_test_user, make_wav_bytes


def test_upload_file(
    client,
    tmp_path,
    monkeypatch,
    test_session_factory,
    jwt_secret_key,
    wav_bytes,
):
    # temporarily sets path to a fake one for test
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id, secret_key=jwt_secret_key, expires_delta=timedelta(minutes=5)
    )

    response = client.post(
        "/uploads",
        files={
            "audio_file": (
                "test.wav",
                wav_bytes,
                "audio/wav",
            )
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200

    # test json content is correct
    data = response.json()
    assert data["filename"] == "test.wav"
    assert data["content_type"] == "audio/wav"
    assert data["id"] != ""

    # test persisted upload metadata and stored file
    upload_id = UUID(data["id"])
    with test_session_factory() as session:
        upload_record = uploads.get_upload(session, upload_id)
        assert upload_record is not None
        assert upload_record.original_filename == "test.wav"
        assert upload_record.user_id == user_id

        saved_file = upload_dir / upload_record.stored_filename
        assert saved_file.exists()
        assert saved_file.read_bytes() == wav_bytes


def test_rejects_unsupported_file_type(
    client,
    tmp_path,
    monkeypatch,
    test_session_factory,
    jwt_secret_key,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id, secret_key=jwt_secret_key, expires_delta=timedelta(minutes=5)
    )

    response = client.post(
        "/uploads",
        files={"audio_file": ("test.txt", b"not audio", "text/plain")},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 415

    data = response.json()
    assert data["detail"] == "Unsupported audio type"
    assert not upload_dir.exists()


def test_upload_requires_authentication(client, tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    response = client.post(
        "/uploads",
        files={
            "audio_file": (
                "test.wav",
                b"fake audio data",
                "audio/wav",
            )
        },
    )

    assert response.status_code == 401
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    assert not upload_dir.exists()


def test_rejects_mp3_upload(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    response = client.post(
        "/uploads",
        files={
            "audio_file": (
                "test.mp3",
                b"fake audio data",
                "audio/mpeg",
            )
        },
        headers=auth_headers,
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported audio type"
    assert not upload_dir.exists()


def test_rejects_non_wav_filename(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    response = client.post(
        "/uploads",
        files={
            "audio_file": (
                "test.mp3",
                b"fake audio data",
                "audio/wav",
            )
        },
        headers=auth_headers,
    )

    assert response.status_code == 415
    assert response.json()["detail"] == "Unsupported audio type"
    assert not upload_dir.exists()


@pytest.mark.parametrize(
    ("limit_offset", "expected_status"),
    [(1, 200), (0, 200), (-1, 413)],
)
def test_upload_enforces_injected_byte_limit(
    tmp_path,
    monkeypatch,
    test_session_factory,
    jwt_secret_key,
    fake_model_session,
    auth_headers,
    wav_bytes,
    limit_offset,
    expected_status,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    payload = wav_bytes
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        max_upload_bytes=len(payload) + limit_offset,
    )
    with TestClient(test_app) as client:
        response = client.post(
            "/uploads",
            files={"audio_file": ("test.wav", payload, "audio/wav")},
            headers=auth_headers,
        )
    assert response.status_code == expected_status
    saved_files = list(upload_dir.glob("*"))
    with test_session_factory() as session:
        upload_count = session.scalar(select(func.count()).select_from(Upload))
    if expected_status == 413:
        assert response.json()["detail"] == "Upload exceeds the maximum allowed size"
        assert saved_files == []
        assert upload_count == 0
    else:
        assert len(saved_files) == 1
        assert saved_files[0].read_bytes() == payload
        assert upload_count == 1


def test_upload_rejects_invalid_wav_contents(
    client, tmp_path, monkeypatch, auth_headers, test_session_factory
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    response = client.post(
        "/uploads",
        files={"audio_file": ("test.wav", b"not an audio file", "audio/wav")},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid or unsupported WAV audio"
    assert list(upload_dir.glob("*")) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


@pytest.mark.parametrize("frames, expected_status", [(8000, 200), (8001, 413)])
def test_upload_enforces_injected_duration_limit(
    tmp_path,
    monkeypatch,
    test_session_factory,
    jwt_secret_key,
    fake_model_session,
    auth_headers,
    frames,
    expected_status,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    payload = make_wav_bytes(frames=frames, sample_rate=8000)
    test_app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        max_audio_duration_seconds=1,
    )
    with TestClient(test_app) as client:
        response = client.post(
            "/uploads",
            files={"audio_file": ("test.wav", payload, "audio/wav")},
            headers=auth_headers,
        )
    assert response.status_code == expected_status
    with test_session_factory() as session:
        upload_count = session.scalar(select(func.count()).select_from(Upload))
    if expected_status == 413:
        assert response.json()["detail"] == "Audio exceeds the maximum allowed duration"
        assert list(upload_dir.glob("*")) == []
        assert upload_count == 0
    else:
        assert upload_count == 1
        assert len(list(upload_dir.glob("*"))) == 1


def test_upload_removes_file_when_validation_fails_unexpectedly(
    client, tmp_path, monkeypatch, auth_headers, test_session_factory, wav_bytes
):
    import app.routers.uploads as upload_routes

    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    def failing_validator(*args, **kwargs):
        raise RuntimeError("Validation failed unexpectedly")

    monkeypatch.setattr(upload_routes, "validate_wav", failing_validator)
    with pytest.raises(RuntimeError, match="Validation failed unexpectedly"):
        client.post(
            "/uploads",
            files={"audio_file": ("test.wav", wav_bytes, "audio/wav")},
            headers=auth_headers,
        )
    assert list(upload_dir.glob("*")) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0
