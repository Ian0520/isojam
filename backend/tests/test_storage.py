from uuid import uuid4
import app.storage as storage

def test_get_job_output_dir_returns_job_specific_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "OUTPUT_DIR", tmp_path)
    job_id = "job-123"
    output_dir = storage.get_job_output_dir(job_id)
    assert output_dir == tmp_path / job_id