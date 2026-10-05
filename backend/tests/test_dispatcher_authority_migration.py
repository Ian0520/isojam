from datetime import UTC, datetime
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import JobAttempt
from app.repositories.job_reservations import reclaim_reservation, reserve_next_job
from tests.factories import create_test_job, create_test_upload, create_test_user

PREVIOUS_REVISION = "c92fd8b7a104"
OLD_ATTEMPT_COLUMNS = (
    "id, job_id, attempt_number, phase, created_at, updated_at, started_at, finished_at"
)


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(
                    f"SELECT {OLD_ATTEMPT_COLUMNS if table == 'job_attempts' else '*'} "
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


def test_authority_upgrade_and_downgrade_preserve_populated_database(
    tmp_path, monkeypatch
):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    output = tmp_path / "guitar.wav"
    output.write_bytes(b"existing-published-audio")
    old_attempts = []
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            for index, phase in enumerate(
                [
                    "reserved",
                    "submitting",
                    "submitted",
                    "running",
                    "result_ready",
                    "uncertain",
                    "succeeded",
                    "failed",
                ]
            ):
                job = create_test_job(
                    session, upload, "completed" if phase == "succeeded" else "pending"
                )
                job.execution_backend = "queued"
                session.flush()
                attempt_id = uuid4()
                old_attempts.append(attempt_id)
                session.execute(
                    text(
                        "INSERT INTO job_attempts (id, job_id, attempt_number, phase) "
                        "VALUES (:id, :job_id, 1, :phase)"
                    ),
                    {"id": attempt_id.hex, "job_id": job.id.hex, "phase": phase},
                )
                if phase == "succeeded":
                    session.execute(
                        text(
                            "INSERT INTO job_outputs (job_id, stem, path) VALUES (:id, 'guitar', :path)"
                        ),
                        {"id": job.id.hex, "path": str(output)},
                    )
                    session.execute(
                        text(
                            "INSERT INTO job_submission_receipts (user_id, key, request_fingerprint, job_id) "
                            "VALUES (:user_id, 'existing-key', :fingerprint, :job_id)"
                        ),
                        {
                            "user_id": user.id.hex,
                            "fingerprint": "a" * 64,
                            "job_id": job.id.hex,
                        },
                    )
            session.commit()
        before = snapshot(engine)
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            for attempt_id in old_attempts:
                stored = session.get(JobAttempt, attempt_id)
                assert stored.dispatcher_id is None
                assert stored.dispatcher_generation == 0
                assert stored.reservation_expires_at is None
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert (
            reclaim_reservation(engine, old_attempts[0], dispatcher_id=uuid4()) is None
        )
        assert reserve_next_job(engine, dispatcher_id=uuid4()) is None
        # Downgrade preserves records, but necessarily discards new authority.
        with Session(engine) as session:
            stored = session.get(JobAttempt, old_attempts[0])
            stored.dispatcher_id = uuid4()
            stored.dispatcher_generation = 3
            stored.reservation_expires_at = datetime(2026, 1, 1, tzinfo=UTC)
            session.commit()
        with_authority = snapshot(engine)
        command.downgrade(config, PREVIOUS_REVISION)
        assert snapshot(engine) == with_authority
        names = {
            column["name"] for column in inspect(engine).get_columns("job_attempts")
        }
        assert not names.intersection(
            {"dispatcher_id", "dispatcher_generation", "reservation_expires_at"}
        )
        command.upgrade(config, "head")
        assert snapshot(engine) == with_authority
        with Session(engine) as session:
            assert session.get(JobAttempt, old_attempts[0]).dispatcher_generation == 0
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert output.read_bytes() == b"existing-published-audio"
    finally:
        engine.dispose()
