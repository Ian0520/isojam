import app.storage as storage
import app.uploads as uploads
import app.processing as processing
import app.jobs as jobs
import app.job_outputs as job_outputs
from app.db_models import Upload

from uuid import uuid4
import pytest
from types import SimpleNamespace
from sqlalchemy import delete

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
    
def test_process_job_completes_with_outputs(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    with test_session_factory() as db:
        upload = uploads.create_upload(
            db,
            "original.wav",
            "test.wav",
        )
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id
        db.commit()

    model_session = FakeSession()
    processing.process_job(job_id, model_session, test_session_factory)

    with test_session_factory() as db:
        job_result = jobs.get_job(db, job_id)
        assert job_result.status == "completed"

        job_output_result = job_outputs.get_job_outputs(db, job_id)
        assert len(job_output_result) == 1
        output = job_output_result[0]
        assert output.stem == "guitar"
        assert output.path == str(output_root / str(job_id) / "test_guitar.wav")

    assert model_session.called
    

def test_process_job_raises_for_missing_job(test_session_factory):
    fake_job_id = uuid4()
    model_session = FakeSession()
    with pytest.raises(KeyError, match="job not found"):
        processing.process_job(fake_job_id, model_session, test_session_factory)
    assert not model_session.called

def test_process_job_fails_when_upload_is_missing(test_session_factory, monkeypatch):
    with test_session_factory() as db:
        upload = uploads.create_upload(db,"song.wav","some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id
        db.commit()

    monkeypatch.setattr(
        uploads,
        "get_upload",
        lambda db, upload_id: None,
    )
    model_session = FakeSession()
    with pytest.raises(RuntimeError, match="upload not found"):
        processing.process_job(job_id, model_session, test_session_factory)
    with test_session_factory() as db: 
        result = jobs.get_job(db, job_id)
        assert result.status == "failed"
    assert not model_session.called

def test_process_job_fails_when_file_is_missing(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

    with test_session_factory() as db:
        upload = uploads.create_upload(
            db,
            "original.wav",
            "missing.wav",
        )
        upload_id = upload.id
        
        job = jobs.create_job(db, upload_id)
        job_id = job.id
        
        db.commit()
    model_session = FakeSession()
    with pytest.raises(FileNotFoundError, match="file not found"):
        processing.process_job(job_id, model_session, test_session_factory)

    with test_session_factory() as db:
        result = jobs.get_job(db, job_id)
        assert result.status == "failed"
    assert not model_session.called

def test_process_job_fails_when_separation_crashes(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    with test_session_factory() as db:
        upload = uploads.create_upload(
            db,
            "original.wav",
            "test.wav",
        )
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id

        db.commit()
    model_session = FailingSession()
    with pytest.raises(RuntimeError, match="inference failed"):
        processing.process_job(job_id, model_session, test_session_factory)
    with test_session_factory() as db:
        result = jobs.get_job(db, job_id)
        assert result.status == "failed"

