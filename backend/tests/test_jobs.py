import app.storage as storage
import app.repositories.jobs as jobs
import app.repositories.uploads as uploads
import app.repositories.job_outputs as job_outputs
from uuid import uuid4, UUID
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

def test_create_job(client, uploaded_file_id, test_session_factory):
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

    job_id = UUID(data["id"])
    with test_session_factory() as db:
        processed_job = jobs.get_job(db, job_id)
        assert processed_job.status == "completed"
        outputs = job_outputs.get_job_outputs(db, job_id)
        assert len(outputs) == 1
        output = outputs[0]
        assert output.stem == "guitar"
        assert output.path == str(storage.get_job_output_dir(job_id) / "test_guitar.wav")



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
    fake_job_id = uuid4()
    response = client.get(f"/jobs/{fake_job_id}/outputs/guitar")

    assert response.status_code == 404
    assert response.json()["detail"] == "The job does not exist"

def test_download_output_for_incomplete_job(client, test_session_factory):
    with test_session_factory() as db:
        upload = uploads.create_upload(db, "song.wav", "some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id

        db.commit()

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
