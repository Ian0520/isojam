from app.db_models import Base, Upload, Job, JobOutput
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError
from uuid import UUID, uuid4
import pytest
from app.database import enable_sqlite_foreign_keys

def test_upload_table_definition():
    table = Upload.__table__

    assert table.name == "uploads"
    assert set(table.columns.keys()) == {
        "id",
        "original_filename",
        "stored_filename",
    }
    assert set(table.primary_key.columns.keys()) == {"id"}

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

def test_upload_can_be_persisted(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)

    Base.metadata.create_all(tmp_engine)
    upload = Upload(original_filename="song.wav",
                    stored_filename="some-uuid.wav",
                    )

    with Session(tmp_engine) as session:
        session.add(upload)
        session.commit()

    with Session(tmp_engine) as session:
        statement = select(Upload).where(Upload.original_filename == "song.wav")
        result = session.scalars(statement).one()
        assert isinstance(result.id, UUID)
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"

def test_job_can_be_persisted(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)

    Base.metadata.create_all(tmp_engine)

    with Session(tmp_engine) as session:
        upload = Upload(original_filename="song.wav",
                    stored_filename="some-uuid.wav",
                    )
        session.add(upload)
        session.commit()
        upload_id = upload.id

        job = Job(upload_id=upload_id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

    with Session(tmp_engine) as session:
        result = session.get(Job, job_id)
        assert job is not None
        assert result.id == job_id
        assert result.upload_id == upload_id
        assert result.status == "pending"

def test_job_rejects_nonexistent_upload(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)
    enable_sqlite_foreign_keys(tmp_engine)

    Base.metadata.create_all(tmp_engine)
    with Session(tmp_engine) as session:
        job = Job(upload_id=uuid4(), status="pending",)
        session.add(job)

        with pytest.raises(IntegrityError):
            session.commit()

def test_job_output_can_be_persisted(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)
    enable_sqlite_foreign_keys(tmp_engine)
    Base.metadata.create_all(tmp_engine)

    with Session(tmp_engine) as session:
        upload = Upload(original_filename="song.wav",
                    stored_filename="some-uuid.wav",
                    )
        session.add(upload)
        session.commit()
        upload_id = upload.id

        job = Job(upload_id=upload_id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

        job_output = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar.wav")
        session.add(job_output)
        session.commit()

    with Session(tmp_engine) as session:
        result = session.get(JobOutput, (job_id, "guitar"))
        assert result is not None
        assert result.job_id == job_id
        assert result.stem == "guitar"
        assert result.path == "/tmp/guitar.wav"

def test_job_output_rejects_duplicate_stem_for_same_job(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)
    enable_sqlite_foreign_keys(tmp_engine)
    Base.metadata.create_all(tmp_engine)

    with Session(tmp_engine) as session:
        upload = Upload(original_filename="song.wav",
                    stored_filename="some-uuid.wav",
                    )
        session.add(upload)
        session.commit()
        upload_id = upload.id

        job = Job(upload_id=upload_id, status="pending")
        session.add(job)
        session.commit()
        job_id = job.id

        job_output_guitar = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar.wav")
        job_output_vocals = JobOutput(job_id=job_id, stem="vocals", path="/tmp/vocals.wav")
        session.add(job_output_guitar)
        session.add(job_output_vocals)
        session.commit()
        
    with Session(tmp_engine) as session:
        job_output_guitar_dup = JobOutput(job_id=job_id, stem="guitar", path="/tmp/guitar_dup.wav")
        session.add(job_output_guitar_dup)
        with pytest.raises(IntegrityError):
            session.commit()