from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import sessionmaker

import app.repositories.jobs as jobs
from app.database import enable_sqlite_foreign_keys
from app.db_models import Base, Job, JobAttempt
from tests.factories import create_test_job, create_test_upload, create_test_user


@pytest.fixture(params=["models", "migration"])
def metadata_session_factory(request, tmp_path, monkeypatch):
    """Exercise database rules in both test-created and Alembic-created schemas."""
    database_url = URL.create("sqlite", database=str(tmp_path / "metadata.db"))
    if request.param == "migration":
        monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(database_url))
        command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    if request.param == "models":
        Base.metadata.create_all(engine)
    try:
        yield sessionmaker(engine)
    finally:
        engine.dispose()


def seed_job(session):
    user = create_test_user(session)
    upload = create_test_upload(session, user)
    return create_test_job(session, upload)


@pytest.mark.parametrize("creation_path", ["ordinary", "limited"])
def test_job_creation_populates_local_backend_and_utc_timestamps(
    metadata_session_factory, creation_path
):
    before = datetime.now(UTC).replace(microsecond=0)
    with metadata_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        if creation_path == "ordinary":
            job = jobs.create_job(session, upload.id)
        else:
            job = jobs.create_job_with_limit(
                session, upload.id, user.id, max_unfinished_jobs=2
            )
        assert job is not None
        job_id = job.id
        session.commit()
    with metadata_session_factory() as session:
        stored = session.get(Job, job_id)
        assert stored.execution_backend == "local"
        assert stored.status == "pending"
        assert stored.created_at.tzinfo is UTC
        assert stored.updated_at.tzinfo is UTC
        assert before <= stored.created_at <= datetime.now(UTC)
        assert stored.updated_at == stored.created_at


def test_attempt_history_preserves_separate_execution_records(metadata_session_factory):
    started = datetime(2026, 1, 2, 12, tzinfo=timezone(timedelta(hours=8)))
    finished = started + timedelta(minutes=2)
    with metadata_session_factory() as session:
        job = seed_job(session)
        job_id = job.id
        first = JobAttempt(
            job_id=job_id,
            attempt_number=1,
            phase="failed",
            created_at=started - timedelta(seconds=1),
            updated_at=finished,
            started_at=started,
            finished_at=finished,
        )
        second = JobAttempt(job_id=job_id, attempt_number=2)
        session.add_all([first, second])
        session.flush()
        first_id, second_id = first.id, second.id
        session.commit()
    with metadata_session_factory() as session:
        first = session.get(JobAttempt, first_id)
        second = session.get(JobAttempt, second_id)
        assert first.job_id == second.job_id == job_id
        assert (first.attempt_number, first.phase) == (1, "failed")
        assert first.started_at == started.astimezone(UTC)
        assert first.finished_at == finished.astimezone(UTC)
        assert first.started_at.tzinfo is UTC
        assert first.finished_at.tzinfo is UTC
        assert (second.attempt_number, second.phase) == (2, "reserved")
        assert second.started_at is None
        assert second.finished_at is None
        # Recording history alone does not change the job's public status.
        assert session.get(Job, job_id).status == "pending"


def test_attempt_numbers_are_unique_within_each_job(metadata_session_factory):
    with metadata_session_factory() as session:
        first_job = seed_job(session)
        second_job = Job(upload_id=first_job.upload_id, status="pending")
        session.add(second_job)
        session.flush()
        first_job_id = first_job.id
        session.add_all(
            [
                JobAttempt(job_id=first_job_id, attempt_number=1),
                JobAttempt(job_id=second_job.id, attempt_number=1),
            ]
        )
        session.commit()
    with metadata_session_factory() as session:
        session.add(JobAttempt(job_id=first_job_id, attempt_number=1))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()


def test_attempt_rejects_missing_job(metadata_session_factory):
    with metadata_session_factory() as session:
        session.add(JobAttempt(job_id=uuid4(), attempt_number=1))
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            session.commit()


@pytest.mark.parametrize("attempt_number", [0, -1])
def test_attempt_rejects_nonpositive_number(metadata_session_factory, attempt_number):
    with metadata_session_factory() as session:
        job = seed_job(session)
        session.add(JobAttempt(job_id=job.id, attempt_number=attempt_number))
        with pytest.raises(IntegrityError, match="ck_job_attempts_positive_number"):
            session.commit()


@pytest.mark.parametrize("phase", ["completed", "invalid"])
def test_attempt_rejects_unknown_phase(metadata_session_factory, phase):
    with metadata_session_factory() as session:
        job = seed_job(session)
        session.add(JobAttempt(job_id=job.id, attempt_number=1, phase=phase))
        with pytest.raises(IntegrityError, match="ck_job_attempts_phase"):
            session.commit()


def test_job_rejects_unknown_execution_backend(metadata_session_factory):
    with metadata_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        session.add(
            Job(upload_id=upload.id, status="pending", execution_backend="disabled")
        )
        with pytest.raises(IntegrityError, match="ck_jobs_execution_backend"):
            session.commit()


def test_queued_backend_can_be_recorded_without_dispatch(metadata_session_factory):
    with metadata_session_factory() as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = Job(upload_id=upload.id, status="pending", execution_backend="queued")
        session.add(job)
        session.flush()
        job_id = job.id
        session.commit()
    with metadata_session_factory() as session:
        stored = session.get(Job, job_id)
        assert stored.execution_backend == "queued"
        assert stored.status == "pending"


@pytest.mark.parametrize("record_kind", ["job", "attempt"])
def test_updates_refresh_timestamp_without_changing_creation_time(
    metadata_session_factory, record_kind
):
    original = datetime(2000, 1, 1, tzinfo=UTC)
    with metadata_session_factory() as session:
        job = seed_job(session)
        if record_kind == "job":
            record = job
        else:
            record = JobAttempt(job_id=job.id, attempt_number=1)
            session.add(record)
        record.created_at = original
        record.updated_at = original
        session.flush()
        record_id = record.id
        session.commit()
    model = Job if record_kind == "job" else JobAttempt
    before = datetime.now(UTC).replace(microsecond=0)
    with metadata_session_factory() as session:
        record = session.get(model, record_id)
        if record_kind == "job":
            record.status = "processing"
        else:
            record.phase = "running"
        session.commit()
    with metadata_session_factory() as session:
        record = session.get(model, record_id)
        assert record.created_at == original
        assert record.updated_at.tzinfo is UTC
        assert before <= record.updated_at <= datetime.now(UTC)


def test_ambiguous_lifecycle_timestamp_is_rejected(metadata_session_factory):
    with metadata_session_factory() as session:
        job = seed_job(session)
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=1,
                started_at=datetime(2026, 1, 2, 12),
            )
        )
        with pytest.raises(StatementError, match="Timestamp must be timezone-aware"):
            session.commit()
