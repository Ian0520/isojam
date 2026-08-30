import app.jobs as jobs
from app.db_models import Job
from tests.factories import create_test_user, create_test_upload
import pytest

from uuid import uuid4

def test_create_job_persists_after_commit(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        upload_id = upload.id

        job = jobs.create_job(session, upload_id)
        assert job.id is not None
        job_id = job.id

        session.commit()

    with test_session_factory() as session:
        result = session.get(Job, job_id)
        assert result is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_get_job_returns_existing_job(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        upload_id = upload.id

        job = jobs.create_job(session, upload_id)
        job_id = job.id

        session.commit()

    with test_session_factory() as session:
        result = jobs.get_job(session, job_id)
        assert result is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_get_job_returns_none_for_missing_job(test_session_factory):
    with test_session_factory() as session:
        result = jobs.get_job(session, uuid4())
        assert result is None

def test_update_job_status_persists_after_commit(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        upload_id = upload.id

        job = jobs.create_job(session, upload_id)
        job_id = job.id

        session.commit()

    with test_session_factory() as session:
        jobs.update_job_status(session, job_id, "processing")
        session.commit()

    with test_session_factory() as session:
        result = jobs.get_job(session, job_id)
        assert result.status == "processing"

def test_update_job_status_rejects_invalid_status(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        upload_id = upload.id

        job = jobs.create_job(session, upload_id)
        job_id = job.id
        session.commit()

    with test_session_factory() as session:
        with pytest.raises(ValueError, match="Invalid job status: invalid_status"):
            jobs.update_job_status(session, job_id, "invalid_status")
    

def test_update_job_status_returns_none_for_missing_job(test_session_factory):
    with test_session_factory() as session:
        result = jobs.update_job_status(session, uuid4(), "processing")
        assert result is None

