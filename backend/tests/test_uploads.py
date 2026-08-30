import app.storage as storage
import app.repositories.uploads as uploads
from uuid import UUID


def test_upload_file(client, tmp_path, monkeypatch, test_session_factory):
    # temporarily sets path to a fake one for test
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
        
        saved_file = upload_dir / upload_record.stored_filename
        assert saved_file.exists()  
        assert saved_file.read_bytes() == b"fake audio data"

def test_rejects_unsupported_file_type(client, tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    monkeypatch.setattr(storage, "UPLOAD_DIR", upload_dir)

    response = client.post("/uploads",
                           files={
                               "audio_file":(
                                    "test.txt",
                                    b"not audio",
                                    "text/plain"
                               )
                           })
    assert response.status_code == 415

    data = response.json()
    assert data["detail"] == "Unsupported audio type"
    assert not upload_dir.exists()