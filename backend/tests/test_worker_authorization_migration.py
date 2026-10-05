from datetime import UTC, datetime, timedelta
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import JobAttempt
from app.repositories.job_reservations import JobReservation, authorize_execution
from tests.factories import create_test_job, create_test_upload, create_test_user

PREVIOUS_REVISION = "d7a6c1039e52"
PREVIOUS_ATTEMPT_COLUMNS = (
    "id, job_id, attempt_number, phase, created_at, updated_at, started_at, finished_at, "
    "dispatcher_id, dispatcher_generation, reservation_expires_at"
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


def test_worker_authority_migration_preserves_data_without_invented_permission(
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
    owner = uuid4()
    expiry = datetime.now(UTC) + timedelta(days=1)
    attempt_ids = []
    reservations = []
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            for phase in [
                "reserved",
                "submitting",
                "submitted",
                "running",
                "result_ready",
                "uncertain",
                "succeeded",
                "failed",
            ]:
                job = create_test_job(
                    session, upload, "completed" if phase == "succeeded" else "pending"
                )
                job.execution_backend = "queued"
                session.flush()
                attempt_id = uuid4()
                attempt_ids.append(attempt_id)
                reservations.append(
                    JobReservation(job.id, attempt_id, 1, owner, 3, expiry)
                )
                session.execute(
                    text(
                        "INSERT INTO job_attempts (id, job_id, attempt_number, phase, dispatcher_id, dispatcher_generation, reservation_expires_at) "
                        "VALUES (:id, :job_id, 1, :phase, :owner, 3, :expiry)"
                    ),
                    {
                        "id": attempt_id.hex,
                        "job_id": job.id.hex,
                        "phase": phase,
                        "owner": owner.hex,
                        "expiry": expiry.replace(tzinfo=None).isoformat(" "),
                    },
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
                            "INSERT INTO job_submission_receipts (user_id, key, request_fingerprint, job_id) VALUES (:user_id, 'existing-key', :fingerprint, :job_id)"
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
            for attempt_id in attempt_ids:
                stored = session.get(JobAttempt, attempt_id)
                assert stored.invocation_id is None
                assert stored.execution_authorization_expires_at is None
                assert stored.dispatcher_id == owner
                assert stored.dispatcher_generation == 3
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        # Old submission intent is not enough: migration cannot safely invent a
        # new deadline and authorize a worker from the former deployment.
        assert not authorize_execution(engine, reservations[1], invocation_id=uuid4())
        with Session(engine) as session:
            stored = session.get(JobAttempt, attempt_ids[3])
            stored.started_at = datetime.now(UTC)
            stored.execution_authorization_expires_at = expiry
            stored.invocation_id = uuid4()
            session.commit()
        with_invocation = snapshot(engine)
        command.downgrade(config, PREVIOUS_REVISION)
        assert snapshot(engine) == with_invocation
        names = {
            column["name"] for column in inspect(engine).get_columns("job_attempts")
        }
        assert not names.intersection(
            {"invocation_id", "execution_authorization_expires_at"}
        )
        command.upgrade(config, "head")
        assert snapshot(engine) == with_invocation
        with Session(engine) as session:
            assert session.get(JobAttempt, attempt_ids[3]).invocation_id is None
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        assert output.read_bytes() == b"existing-published-audio"
    finally:
        engine.dispose()
