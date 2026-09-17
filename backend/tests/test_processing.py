import app.storage as storage
import app.repositories.uploads as uploads
import app.processing as processing
import app.repositories.jobs as jobs
import app.repositories.job_outputs as job_outputs
from tests.factories import (create_test_user, 
                            create_test_upload, 
                            create_test_job)

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
    
def test_process_job_completes_with_outputs(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, 
                                    user,
                                    stored_filename="test.wav",)
        job = create_test_job(session, upload)
        job_id = job.id
        session.commit()

    model_session = FakeSession()
    processing.process_job(job_id, model_session, test_session_factory)

    with test_session_factory() as session:
        job_result = jobs.get_job(session, job_id)
        assert job_result.status == "completed"

        job_output_result = job_outputs.get_job_outputs(session, job_id)
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
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        session.commit()

    monkeypatch.setattr(
        uploads,
        "get_upload",
        lambda session, upload_id: None,
    )
    model_session = FakeSession()
    with pytest.raises(RuntimeError, match="upload not found"):
        processing.process_job(job_id, model_session, test_session_factory)
    with test_session_factory() as session: 
        result = jobs.get_job(session, job_id)
        assert result.status == "failed"
    assert not model_session.called

def test_process_job_fails_when_file_is_missing(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        
        session.commit()
    model_session = FakeSession()
    with pytest.raises(FileNotFoundError, match="file not found"):
        processing.process_job(job_id, model_session, test_session_factory)

    with test_session_factory() as session:
        result = jobs.get_job(session, job_id)
        assert result.status == "failed"
    assert not model_session.called

def test_process_job_fails_when_separation_crashes(tmp_path, monkeypatch, test_session_factory):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    input_path = tmp_path / "test.wav"
    input_path.write_bytes(b"fake audio file")

    output_root = tmp_path / "outputs"
    monkeypatch.setattr(storage, "OUTPUT_DIR", output_root)

    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, 
                                    user,
                                    stored_filename="test.wav",)
        job = create_test_job(session, upload)
        job_id = job.id
        session.commit()
    model_session = FailingSession()
    with pytest.raises(RuntimeError, match="inference failed"):
        processing.process_job(job_id, model_session, test_session_factory)
    with test_session_factory() as session:
        result = jobs.get_job(session, job_id)
        assert result.status == "failed"

