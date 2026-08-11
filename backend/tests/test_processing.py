import app.storage as storage
import app.uploads as uploads
import app.processing as processing
import app.jobs as jobs
from uuid import uuid4
import pytest

def test_process_job_sets_status_to_processing(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    upload = uploads.create_upload("original.wav", "test.wav")
    upload_id = upload["id"]

    job = jobs.create_job(upload_id)
    job_id = job["id"]

    assert processing.process_job(job_id)["status"] == "processing"
    assert jobs.get_job(job_id)["status"] == "processing"

    

def test_process_job_raises_for_missing_job():
    fake_job_id = str(uuid4())
    with pytest.raises(KeyError, match="job not found"):
        processing.process_job(fake_job_id)

def test_process_job_fails_when_upload_is_missing():
    fake_upload_id = str(uuid4())
    job = jobs.create_job(fake_upload_id)
    job_id = job["id"]
    with pytest.raises(RuntimeError, match="upload not found"):
        processing.process_job(job_id)
    assert jobs.get_job(job_id)["status"] == "failed"

def test_process_job_fails_when_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    upload = uploads.create_upload("original.wav", "missing.wav")
    upload_id = upload["id"]

    job = jobs.create_job(upload_id)
    job_id = job["id"]

    with pytest.raises(FileNotFoundError, match="file not found"):
        processing.process_job(job_id)
    assert jobs.get_job(job_id)["status"] == "failed"
    