from fastapi.testclient import TestClient
from app.main import app
import app.storage as storage
from uuid import uuid4
import pytest

client = TestClient(app)

@pytest.fixture
def uploaded_file_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    upload_response = client.post("/uploads",
                                  files={
                                      "audio_file": (
                                          "test.wav",
                                          b"fake audio data",
                                          "audio/wav",
                                    )
                                  })

    assert upload_response.status_code == 200

    upload_id = upload_response.json()["id"]
    return upload_id

def test_create_job(uploaded_file_id):
    upload_id = uploaded_file_id

    create_response = client.post("/jobs",
                                      json={
                                          "upload_id": upload_id
                                          })
    assert create_response.status_code == 200

    data = create_response.json()
    assert data["id"] != ""
    assert data["status"] == "pending"
    assert data["upload_id"] == upload_id

def test_create_job_not_found():
    fake_upload_id = str(uuid4())
    response = client.post("/jobs",
                           json={
                               "upload_id": fake_upload_id
                           })
    assert response.status_code == 404
    data = response.json()
    assert data["detail"] == "The upload does not exist"


def test_get_job(uploaded_file_id):
    upload_id = uploaded_file_id   
    
    create_response = client.post("/jobs",
                                  json={
                                      "upload_id": upload_id
                                  })
    assert create_response.status_code == 200
    created_job = create_response.json()
    job_id = created_job["id"]
    get_response = client.get(f"/jobs/{job_id}")

    assert get_response.status_code == 200

    retrieved_job = get_response.json()
    assert retrieved_job["id"] == job_id
    assert retrieved_job["status"] == "pending"
    assert retrieved_job["upload_id"] == upload_id

def test_get_job_not_found():
    fake_job_id = str(uuid4())
    response = client.get(f"/jobs/{fake_job_id}")

    assert response.status_code == 404

    data = response.json()
    assert data["detail"] == "The requested job does not exist"