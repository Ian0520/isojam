import app.storage as storage
import app.jobs as jobs
from uuid import uuid4
import pytest

@pytest.fixture
def uploaded_file_id(client, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

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

def test_create_job(client, uploaded_file_id):
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
    assert data["outputs"] == {}

    job_id = data["id"]
    processed_job = jobs.get_job(job_id)
    assert processed_job["status"] == "completed"
    assert processed_job["outputs"]["guitar"] ==  str(storage.get_job_output_dir(job_id) / "test_guitar.wav")



def test_create_job_not_found(client):
    fake_upload_id = str(uuid4())
    response = client.post("/jobs",
                           json={
                               "upload_id": fake_upload_id
                           })
    assert response.status_code == 404
    data = response.json()
    assert data["detail"] == "The upload does not exist"


def test_get_job(client, uploaded_file_id):
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
    assert retrieved_job["status"] == "completed"
    assert retrieved_job["upload_id"] == upload_id

    assert retrieved_job["outputs"]["guitar"] == f"/jobs/{job_id}/outputs/guitar"

def test_get_job_not_found(client):
    fake_job_id = str(uuid4())
    response = client.get(f"/jobs/{fake_job_id}")

    assert response.status_code == 404

    data = response.json()
    assert data["detail"] == "The requested job does not exist"

def test_update_job_status(uploaded_file_id):
    job= jobs.create_job(uploaded_file_id)
    job_id = job["id"]
    updated_job = jobs.update_job_status(job_id, "processing")

    assert jobs.get_job(job_id)["status"] == "processing"
    assert updated_job["status"] == "processing"

def test_update_job_status_invalid(uploaded_file_id):
    job = jobs.create_job(uploaded_file_id)
    job_id = job["id"]

    with pytest.raises(ValueError, match=f"Invalid job status: banana"):
        jobs.update_job_status(job_id, "banana")


def test_update_job_status_not_found():
    fake_job_id = str(uuid4())
    job = jobs.update_job_status(fake_job_id, "processing")
    assert job is None

def test_download_job_output(client, uploaded_file_id):
    upload_id = uploaded_file_id
    create_response = client.post("/jobs",
                    json={
                        "upload_id": upload_id
                    })
    assert create_response.status_code == 200
    
    job_id = create_response.json()["id"]
    output_path = storage.get_job_output_dir(job_id) / "test_guitar.wav"
    output_path.write_bytes(b"fake audio")

    get_response = client.get(f"/jobs/{job_id}/outputs/guitar")
    assert get_response.status_code == 200
    assert get_response.content == b"fake audio"

    
def test_download_output_for_missing_job(client):
    response = client.get("/jobs/job-123/outputs/guitar")

    assert response.status_code == 404
    assert response.json()["detail"] == "The job does not exist"

def test_download_output_for_incomplete_job(client):
    job = jobs.create_job("upload-123")
    job_id = job["id"]

    response = client.get(f"/jobs/{job_id}/outputs/guitar")

    assert response.status_code == 409
    assert response.json()["detail"] == "The job is not completed"


def test_download_missing_stem(client, uploaded_file_id):
    upload_id = uploaded_file_id
    create_response = client.post("/jobs",
                                  json={"upload_id": upload_id})
    assert create_response.status_code == 200
    job_id = create_response.json()["id"]

    download_response = client.get(f"/jobs/{job_id}/outputs/violin")
    assert download_response.status_code == 404
    assert download_response.json()["detail"] == "The requested stem does not exist"

def test_download_output_when_file_is_missing(client, uploaded_file_id):
    upload_id = uploaded_file_id
    create_response = client.post("/jobs",
                                  json={"upload_id": upload_id})
    assert create_response.status_code == 200
    job_id = create_response.json()["id"]

    download_response = client.get(f"/jobs/{job_id}/outputs/guitar")

    assert download_response.status_code == 404
    assert download_response.json()["detail"] == "The output file is missing"
