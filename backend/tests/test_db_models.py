from app.db_models import Upload, Job, JobOutput, User
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from uuid import UUID, uuid4
import pytest

def create_test_user(session, email="user@example.com"):
    user = User(
        email=email,
        password_hash="some-hash",
    )
    session.add(user)
    session.flush()
    return user

def create_test_upload(
    session,
    user,
    original_filename="song.wav",
    stored_filename="some-uuid.wav",
):
    upload = Upload(user_id=user.id,
                    original_filename=original_filename,
                    stored_filename=stored_filename,
                    )
    session.add(upload)
    session.flush()
    return upload

def create_test_job(session, upload, status="pending"):
    job = Job(upload_id=upload.id, status="pending")
    session.add(job)
    session.flush()
    return job

def test_upload_table_definition():
    table = Upload.__table__

    assert table.name == "uploads"
    assert set(table.columns.keys()) == {
        "id",
        "user_id",
        "original_filename",
        "stored_filename",
    }
    assert set(table.primary_key.columns.keys()) == {"id"}

    assert not table.columns["user_id"].nullable
    assert not table.columns["original_filename"].nullable
    assert not table.columns["stored_filename"].nullable

def test_job_table_definition():
    table = Job.__table__

    assert table.name == "jobs"
    assert set(table.columns.keys()) == {
        "id",
        "upload_id",
        "status",
    }
    assert set(table.primary_key.columns.keys()) == {"id"}

    upload_id_column = table.columns["upload_id"]
    foreign_key_targets = {
        foreign_key.target_fullname
        for foreign_key in upload_id_column.foreign_keys
    }
    assert foreign_key_targets == {"uploads.id"}

    assert not table.columns["upload_id"].nullable
    assert not table.columns["status"].nullable

def test_job_output_table_definition():
    table = JobOutput.__table__

    assert table.name == "job_outputs"
    assert set(table.columns.keys()) == {
        "job_id",
        "stem",
        "path",
    }

    assert set(table.primary_key.columns.keys()) == {"job_id", "stem"}

    job_id_column = table.columns["job_id"]
    foreign_key_targets = {
        foreign_key.target_fullname
        for foreign_key in job_id_column.foreign_keys
    }
    assert foreign_key_targets == {"jobs.id"}

    assert not table.columns["job_id"].nullable
    assert not table.columns["stem"].nullable
    assert not table.columns["path"].nullable

def test_upload_can_be_persisted(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = Upload(user_id=user.id,
                        original_filename="song.wav",
                        stored_filename="some-uuid.wav",
                        )
        session.add(upload)
        session.commit()

    with test_session_factory() as session:
        statement = select(Upload).where(Upload.original_filename == "song.wav")
        result = session.scalars(statement).one()
        assert isinstance(result.id, UUID)
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"

def test_job_can_be_persisted(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        upload_id = upload.id

        job = Job(upload_id=upload_id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

    with test_session_factory() as session:
        result = session.get(Job, job_id)
        assert result is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_job_rejects_nonexistent_upload(test_session_factory):
    with test_session_factory() as session:
        job = Job(upload_id=uuid4(), status="pending",)
        session.add(job)

        with pytest.raises(IntegrityError):
            session.commit()

def test_job_output_can_be_persisted(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)  
        job = create_test_job(session, upload)
        job_id = job.id

        job_output = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar.wav")
        session.add(job_output)
        session.commit()

    with test_session_factory() as session:
        result = session.get(JobOutput, (job_id, "guitar"))
        assert result is not None
        assert result.job_id == job_id
        assert result.stem == "guitar"
        assert result.path == "/tmp/guitar.wav"

def test_job_output_rejects_duplicate_stem_for_same_job(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = create_test_job(session, upload)
        job_id = job.id

        job_output_guitar = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar.wav")
        job_output_vocals = JobOutput(job_id=job_id, stem="vocals", path="/tmp/vocals.wav")
        session.add(job_output_guitar)
        session.add(job_output_vocals)
        session.commit()
        
    with test_session_factory() as session:
        job_output_guitar_dup = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar_dup.wav")
        session.add(job_output_guitar_dup)
        with pytest.raises(IntegrityError):
            session.commit()

def test_user_table_definition():
    table = User.__table__

    assert table.name == "users"
    assert set(table.columns.keys()) == {
        "id",
        "email",
        "password_hash",
    }

    assert set(table.primary_key.columns.keys()) == {"id"}

    assert not table.columns["email"].nullable
    assert table.columns["email"].unique

    assert not table.columns["password_hash"].nullable

def test_upload_has_user_foreign_key():
    table = Upload.__table__

    upload_id_column = table.columns["user_id"]
    assert not upload_id_column.nullable
    
    foreign_key_targets = {
        foreign_key.target_fullname
        for foreign_key in upload_id_column.foreign_keys
    }
    assert foreign_key_targets == {"users.id"}

def test_upload_rejects_nonexistent_user(test_session_factory):
    with test_session_factory() as session:
        upload = Upload(id=uuid4(), original_filename="song.wav", stored_filename="some-uuid.wav",)
        session.add(upload)
        with pytest.raises(IntegrityError):
            session.commit()
