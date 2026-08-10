from fastapi.testclient import TestClient
from app.main import app
import app.storage as storage

client = TestClient(app)

def test_upload_file(tmp_path, monkeypatch):
    # temporarily sets path to a fake one for test
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

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
    assert data["stored_filename"].endswith(".wav")

    # test that the file exists
    saved_file = tmp_path / data["stored_filename"]  
    assert saved_file.exists()  
    assert saved_file.read_bytes() == b"fake audio data"

def test_rejects_unsupported_file_type(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

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
    assert not list(tmp_path.iterdir())