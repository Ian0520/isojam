import app.storage as storage
import app.uploads as uploads
import app.processing as processing
import app.jobs as jobs
from uuid import uuid4
import pytest
from types import SimpleNamespace

class FakeSession:
    def __init__(self):
        self.called = False
    def infer(self, input_folder, *, store_dir):
        self.called = True

        fake_guitar_output = SimpleNamespace(
            output_id="guitar",
            output_path=store_dir / "test_guitar.wav",
        )
        manifest = SimpleNamespace(outputs=[fake_guitar_output])
        return manifest

class FailingSession:
    def infer(self, input_folder, *, store_dir):
        raise RuntimeError("inference failed")
    
def test_process_job_completes_with_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    upload = uploads.create_upload("original.wav", "test.wav")
    upload_id = upload["id"]

    job = jobs.create_job(upload_id)
    job_id = job["id"]

    session = FakeSession()
    result = processing.process_job(job_id, session)

    assert session.called
    assert result["status"] == "completed"
    assert jobs.get_job(job_id)["status"] == "completed"
    assert result["outputs"]["guitar"] == str(output_root / job_id / "test_guitar.wav")
    

def test_process_job_raises_for_missing_job():
    fake_job_id = str(uuid4())
    session = FakeSession()
    with pytest.raises(KeyError, match="job not found"):
        processing.process_job(fake_job_id, session)
    assert not session.called

def test_process_job_fails_when_upload_is_missing():
    fake_upload_id = str(uuid4())
    job = jobs.create_job(fake_upload_id)
    job_id = job["id"]
    session = FakeSession()
    with pytest.raises(RuntimeError, match="upload not found"):
        processing.process_job(job_id, session)
    assert jobs.get_job(job_id)["status"] == "failed"
    assert not session.called

def test_process_job_fails_when_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    upload = uploads.create_upload("original.wav", "missing.wav")
    upload_id = upload["id"]

    job = jobs.create_job(upload_id)
    job_id = job["id"]

    session = FakeSession()
    with pytest.raises(FileNotFoundError, match="file not found"):
        processing.process_job(job_id, session)
    assert jobs.get_job(job_id)["status"] == "failed"
    assert not session.called

def test_process_job_fails_when_separation_crashes(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    upload = uploads.create_upload("original.wav", "test.wav")
    upload_id = upload["id"]

    job = jobs.create_job(upload_id)
    job_id = job["id"]

    session = FailingSession()
    with pytest.raises(RuntimeError, match="inference failed"):
        processing.process_job(job_id, session)
    assert jobs.get_job(job_id)["status"] == "failed"

