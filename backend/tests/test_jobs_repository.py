import app.uploads as uploads
import app.jobs as jobs
from app.db_models import Job

import pytest
from uuid import uuid4

def test_create_job_persists_after_commit(test_session_factory):
    with test_session_factory() as db:
        upload = uploads.create_upload(db, "song.wav", "some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        assert job.id is not None
        job_id = job.id

        db.commit()

    with test_session_factory() as db:
        result = db.get(Job, job_id)
        assert result is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_get_job_returns_existing_job(test_session_factory):
    with test_session_factory() as db:
        upload = uploads.create_upload(db, "song.wav", "some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id

        db.commit()

    with test_session_factory() as db:
        result = jobs.get_job(db, job_id)
        assert result is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_get_job_returns_none_for_missing_job(test_session_factory):
    with test_session_factory() as db:
        result = jobs.get_job(db, uuid4())
        assert result is None

def test_update_job_status_persists_after_commit(test_session_factory):
    with test_session_factory() as db:
        upload = uploads.create_upload(db, "song.wav", "some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id

        db.commit()

    with test_session_factory() as db:
        jobs.update_job_status(db, job_id, "processing")
        db.commit()

    with test_session_factory() as db:
        result = jobs.get_job(db, job_id)
        assert result.status == "processing"

def test_update_job_status_rejects_invalid_status(test_session_factory):
    with test_session_factory() as db:
        upload = uploads.create_upload(db, "song.wav", "some-uuid.wav")
        upload_id = upload.id

        job = jobs.create_job(db, upload_id)
        job_id = job.id
        db.commit()

    with test_session_factory() as db:
        with pytest.raises(ValueError, match="Invalid job status: invalid_status"):
            jobs.update_job_status(db, job_id, "invalid_status")
    

def test_update_job_status_returns_none_for_missing_job(test_session_factory):
    with test_session_factory() as db:
        result = jobs.update_job_status(db, uuid4(), "processing")
        assert result is None

