from datetime import timedelta
from uuid import UUID

import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

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


def test_rejects_invalid_mp3_upload(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    test_session_factory,
    encoded_audio,
):
    # Ensure conversion tools are available before asserting a decoding failure.
    encoded_audio()
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

    assert response.status_code == 422
    assert list(upload_dir.iterdir()) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_rejects_mismatched_filename_and_content_type(
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


@pytest.mark.parametrize("suffix", [".wav", ".flac"])
@pytest.mark.parametrize("failure_step", ["repository", "commit"])
def test_upload_removes_file_when_metadata_persistence_fails(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    test_session_factory,
    wav_bytes,
    failure_step,
    suffix,
    encoded_audio,
):
    payload = wav_bytes if suffix == ".wav" else encoded_audio(suffix).read_bytes()
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    existing_file = upload_dir / "existing.wav"
    existing_file.write_bytes(wav_bytes)
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    persistence_error = SQLAlchemyError("Simulated upload persistence failure")

    if failure_step == "repository":
        original_create_upload = uploads.create_upload

        def failing_create_upload(*args, **kwargs):
            original_create_upload(*args, **kwargs)
            raise persistence_error

        monkeypatch.setattr(uploads, "create_upload", failing_create_upload)
    else:

        def failing_commit(session):
            raise persistence_error

        monkeypatch.setattr(test_session_factory.class_, "commit", failing_commit)

    with pytest.raises(SQLAlchemyError) as captured_error:
        client.post(
            "/uploads",
            files={"audio_file": (f"test{suffix}", payload, f"audio/{suffix[1:]}")},
            headers=auth_headers,
        )
    assert captured_error.value is persistence_error
    assert list(upload_dir.iterdir()) == [existing_file]
    assert existing_file.read_bytes() == wav_bytes
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


@pytest.mark.parametrize(
    "suffix, content_type",
    [
        (".mp3", "audio/mpeg"),
        (".m4a", "audio/mp4"),
        (".aac", "audio/aac"),
        (".flac", "audio/flac"),
    ],
)
def test_upload_normalizes_audio_and_preserves_display_filename(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    test_session_factory,
    encoded_audio,
    suffix,
    content_type,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    source = encoded_audio(suffix)
    response = client.post(
        "/uploads",
        headers=auth_headers,
        files={
            "audio_file": (f"My recording{suffix}", source.read_bytes(), content_type)
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == f"My recording{suffix}"
    assert response.json()["content_type"] == content_type
    with test_session_factory() as session:
        record = session.get(Upload, UUID(response.json()["id"]))
        assert record.original_filename == f"My recording{suffix}"
        assert record.stored_filename.endswith(".wav")
        output = upload_dir / record.stored_filename
    assert list(upload_dir.iterdir()) == [output]
    assert sf.info(output).samplerate == 44100
    assert sf.info(output).subtype == "FLOAT"


@pytest.mark.parametrize(
    "limit, value",
    [
        ("max_converted_audio_bytes", 10000),
        ("max_audio_duration_seconds", 1),
    ],
)
def test_upload_removes_source_and_partial_conversion_when_limit_exceeded(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    encoded_audio,
    test_session_factory,
    limit,
    value,
):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(client.app.state, limit, value)
    source = encoded_audio(".flac", frames=44101)
    response = client.post(
        "/uploads",
        headers=auth_headers,
        files={"audio_file": ("source.flac", source.read_bytes(), "audio/flac")},
    )
    assert response.status_code == 413
    assert list(upload_dir.iterdir()) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_upload_collision_preserves_existing_normalized_file(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    encoded_audio,
    test_session_factory,
):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    monkeypatch.setattr(storage, "uuid4", lambda: "existing")
    existing = upload_dir / "existing.wav"
    existing.write_bytes(b"keep this recording")
    source = encoded_audio()
    with pytest.raises(FileExistsError):
        client.post(
            "/uploads",
            headers=auth_headers,
            files={"audio_file": ("source.mp3", source.read_bytes(), "audio/mpeg")},
        )
    assert list(upload_dir.iterdir()) == [existing]
    assert existing.read_bytes() == b"keep this recording"
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


@pytest.mark.parametrize("failure", ["unavailable", "busy", "unexpected"])
def test_upload_cleans_source_when_converter_cannot_run(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    test_session_factory,
    failure,
):
    import app.routers.uploads as upload_routes
    from app.audio_conversion import ConversionBusyError, ConversionUnavailableError

    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)
    errors = {
        "unavailable": ConversionUnavailableError("Conversion unavailable"),
        "busy": ConversionBusyError("Conversion busy"),
        "unexpected": RuntimeError("Unexpected failure"),
    }

    def failing_converter(*args, **kwargs):
        raise errors[failure]

    monkeypatch.setattr(upload_routes, "convert_to_wav", failing_converter)

    def upload():
        return client.post(
            "/uploads",
            headers=auth_headers,
            files={"audio_file": ("source.mp3", b"source", "audio/mpeg")},
        )

    if failure == "unexpected":
        with pytest.raises(RuntimeError, match="Unexpected failure"):
            upload()
    else:
        assert upload().status_code == 503
    assert list(upload_dir.iterdir()) == []
    with test_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_upload_conversion_runs_without_open_database_transaction(
    client,
    tmp_path,
    monkeypatch,
    auth_headers,
    encoded_audio,
    test_session_factory,
):
    import app.routers.uploads as upload_routes
    from app.database import get_db

    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path / "uploads")
    source = encoded_audio(".flac")
    original_converter = upload_routes.convert_to_wav
    with test_session_factory() as session:

        def same_session():
            yield session

        def checked_converter(*args, **kwargs):
            assert not session.in_transaction()
            return original_converter(*args, **kwargs)

        client.app.dependency_overrides[get_db] = same_session
        monkeypatch.setattr(upload_routes, "convert_to_wav", checked_converter)
        try:
            response = client.post(
                "/uploads",
                headers=auth_headers,
                files={
                    "audio_file": ("source.flac", source.read_bytes(), "audio/flac")
                },
            )
            assert response.status_code == 200
        finally:
            client.app.dependency_overrides.pop(get_db)
