import app.job_outputs as job_outputs
from app.db_models import JobOutput
from tests.factories import (create_test_user, 
                            create_test_upload, 
                            create_test_job)


def test_create_job_output_persists_after_commit(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        
        stem = "guitar"
        path = "/some/path/guitar.wav"
        job_output = job_outputs.create_job_output(session, job_id, stem, path)
        assert job_output.job_id == job_id
        session.commit()

    with test_session_factory() as session:
        result = session.get(JobOutput, (job_id, stem))
        assert result is not None
        assert result.job_id == job_id
        assert result.stem == stem
        assert result.path == path

def test_get_job_output_returns_existing_output(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        
        stem = "guitar"
        path = "/some/path/guitar.wav"
        job_outputs.create_job_output(session, job_id, stem, path)
        session.commit()

    with test_session_factory() as session:
        result = job_outputs.get_job_output(session, job_id, stem)
        assert result is not None
        assert result.job_id == job_id
        assert result.stem == stem
        assert result.path == path

def test_get_job_output_returns_none_for_missing_output(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        
        stem = "guitar"
        path = "/some/path/guitar.wav"
        job_outputs.create_job_output(session, job_id, stem, path)
        session.commit()

    with test_session_factory() as session:
        result = job_outputs.get_job_output(session, job_id, "missing_stem")
        assert result is None

def test_get_job_outputs_returns_outputs_for_job(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id

        job_outputs.create_job_output(session, job_id, "guitar", "/some/path/guitar.wav")
        job_outputs.create_job_output(session, job_id, "vocals", "/some/path/vocals.wav")
        session.commit()

    with test_session_factory() as session:
        results = job_outputs.get_job_outputs(session, job_id)
        assert len(results) == 2

        outputs_by_stem = {output.stem: output for output in results}
        assert outputs_by_stem["guitar"].job_id == job_id
        assert outputs_by_stem["guitar"].stem == "guitar"
        assert outputs_by_stem["guitar"].path == "/some/path/guitar.wav"

        assert outputs_by_stem["vocals"].job_id == job_id
        assert outputs_by_stem["vocals"].stem == "vocals"
        assert outputs_by_stem["vocals"].path == "/some/path/vocals.wav"

def test_get_job_outputs_returns_empty_list_when_none_exist(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id
        
        session.commit()
    with test_session_factory() as session:
        result = job_outputs.get_job_outputs(session, job_id)
        assert result == []