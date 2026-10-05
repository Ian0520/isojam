from datetime import timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput, JobSubmissionReceipt
from app.repositories import job_reservations as reservations
from tests.factories import (
    create_test_job,
    create_test_upload,
    create_test_user,
    make_wav_bytes,
)

PREVIOUS_REVISION = "e4f21c8a906b"
PREVIOUS_ATTEMPT_COLUMNS = (
    "id, job_id, attempt_number, phase, created_at, updated_at, started_at, finished_at, "
    "dispatcher_id, dispatcher_generation, reservation_expires_at, "
    "execution_authorization_expires_at, invocation_id"
)


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(
                    f"SELECT {PREVIOUS_ATTEMPT_COLUMNS if table == 'job_attempts' else '*'} "
                    f"FROM {table} ORDER BY 1, 2"
                )
            ).all()
            for table in (
                "users",
                "uploads",
                "jobs",
                "job_attempts",
                "job_outputs",
                "job_submission_receipts",
            )
        }


def test_heartbeat_migration_preserves_execution_evidence_and_existing_audio(
    tmp_path, monkeypatch, clock
):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    output_path = tmp_path / "guitar.wav"
    output_bytes = make_wav_bytes()
    output_path.write_bytes(output_bytes)
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            session.add(
                Job(upload_id=upload.id, status="pending", execution_backend="queued")
            )
            completed = create_test_job(session, upload, "completed")
            session.add(
                JobOutput(job_id=completed.id, stem="guitar", path=str(output_path))
            )
            session.add(
                JobSubmissionReceipt(
                    user_id=user.id,
                    key="old-request",
                    request_fingerprint="a" * 64,
                    job_id=completed.id,
                )
            )
            legacy_attempt_id = uuid4()
            session.execute(
                text(
                    "INSERT INTO job_attempts (id, job_id, attempt_number, phase) VALUES (:id, :job_id, 1, 'succeeded')"
                ),
                {"id": legacy_attempt_id.hex, "job_id": completed.id.hex},
            )
            session.commit()
        # These Core operations use only existing columns, establishing actual
        # stage-one execution evidence in the previous schema before upgrade.
        reservation = reservations.reserve_next_job(engine, dispatcher_id=uuid4())
        assert reservations.begin_submission(engine, reservation)
        invocation_id = uuid4()
        assert reservations.authorize_execution(
            engine, reservation, invocation_id=invocation_id
        )
        before = snapshot(engine)
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            running = session.get(JobAttempt, reservation.attempt_id)
            assert running.invocation_id == invocation_id
            assert running.phase == "running"
            assert running.last_heartbeat_at is None
            assert session.get(JobAttempt, legacy_attempt_id).last_heartbeat_at is None
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        clock.now += timedelta(seconds=5)
        assert reservations.record_heartbeat(
            engine, reservation, invocation_id=invocation_id
        )
        with_heartbeat = snapshot(engine)
        command.downgrade(config, PREVIOUS_REVISION)
        assert snapshot(engine) == with_heartbeat
        assert "last_heartbeat_at" not in {
            column["name"] for column in inspect(engine).get_columns("job_attempts")
        }
        # Invocation uniqueness must survive the heartbeat table recreation.
        assert any(
            item["column_names"] == ["invocation_id"]
            for item in inspect(engine).get_unique_constraints("job_attempts")
        )
        command.upgrade(config, "head")
        assert snapshot(engine) == with_heartbeat
        with Session(engine) as session:
            running = session.get(JobAttempt, reservation.attempt_id)
            assert running.invocation_id == invocation_id
            assert running.last_heartbeat_at is None
            assert session.get(Job, reservation.job_id).status == "processing"
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert output_path.read_bytes() == output_bytes
    finally:
        engine.dispose()
