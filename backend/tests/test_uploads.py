import app.storage as storage
import app.repositories.uploads as uploads
from app.security import create_access_token
from tests.factories import create_test_user

from uuid import UUID
from datetime import timedelta


def test_upload_file(
    client,
    tmp_path,
    monkeypatch,
    test_session_factory,
    jwt_secret_key,
):
    # temporarily sets path to a fake one for test
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()

    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5)
    )


    response = client.post(
        "/uploads", 
        files={
            "audio_file": (
                "test.wav",
                b"fake audio data",
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
        assert saved_file.read_bytes() == b"fake audio data"


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
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5)
    )

    response = client.post(
        "/uploads",
        files={
            "audio_file":(
                "test.txt",
                b"not audio",
                "text/plain"
            )
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == 415

    data = response.json()
    assert data["detail"] == "Unsupported audio type"
    assert not upload_dir.exists()


def test_upload_requires_authentication(client, tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    response = client.post("/uploads", 
                           files={
                               "audio_file": (
                                   "test.wav",
                                   b"fake audio data",
                                   "audio/wav",
                               )
                           })

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
