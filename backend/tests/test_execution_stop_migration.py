from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput
from app.fake_worker import create_fake_workspace
from app.publication import publish_result
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore, ResultIdentity
from tests.factories import create_test_upload, create_test_user

PREVIOUS_REVISION = "a4c9e7201b63"
STOP_COLUMNS = {
    "local_worker_id",
    "local_worker_pid",
    "execution_stopped_at",
    "execution_exit_code",
}


def snapshot(engine, old_columns):
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(f"SELECT {', '.join(columns)} FROM {table} ORDER BY 1, 2")
            ).all()
            for table, columns in old_columns.items()
        }


def test_stop_migration_preserves_old_publications_without_inventing_exit_evidence(
    tmp_path, monkeypatch, clock
):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    try:
        old_columns = {
            table: [column["name"] for column in inspect(engine).get_columns(table)]
            for table in (
                "users",
                "uploads",
                "jobs",
                "job_attempts",
                "job_outputs",
                "job_submission_receipts",
            )
        }
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            for _ in range(2):
                session.add(
                    Job(
                        upload_id=upload.id,
                        status="pending",
                        execution_backend="queued",
                    )
                )
            session.commit()
        store = LocalResultStore(tmp_path / "results")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        create_fake_workspace(workspace)
        deliveries = []
        for completed in (True, False):
            token = reservations.reserve_next_job(engine, dispatcher_id=uuid4())
            assert reservations.begin_submission(engine, token)
            invocation_id = uuid4()
            assert reservations.authorize_execution(
                engine, token, invocation_id=invocation_id
            )
            identity = ResultIdentity(
                job_id=token.job_id,
                attempt_id=token.attempt_id,
                invocation_id=invocation_id,
            )
            bundle = store.write_bundle(identity, workspace)
            # Seed old rows without invoking new repository code that refers to
            # stop columns. The old release did not persist controller exit proof.
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE job_attempts SET last_heartbeat_at = started_at WHERE id = :id"
                    ),
                    {"id": token.attempt_id.hex},
                )
                if completed:
                    connection.execute(
                        text(
                            "UPDATE job_attempts SET phase = 'succeeded', finished_at = started_at, "
                            "result_manifest_key = :key, result_manifest_sha256 = :sha WHERE id = :id"
                        ),
                        {
                            "id": token.attempt_id.hex,
                            "key": bundle.manifest_key,
                            "sha": bundle.manifest_sha256,
                        },
                    )
                    connection.execute(
                        text("UPDATE jobs SET status = 'completed' WHERE id = :id"),
                        {"id": token.job_id.hex},
                    )
                    connection.execute(
                        JobOutput.__table__.insert(),
                        [
                            {
                                "job_id": token.job_id,
                                "stem": output.stem,
                                "path": str(output.path),
                            }
                            for output in bundle.outputs
                        ],
                    )
            deliveries.append((token, invocation_id, identity))
        before = snapshot(engine, old_columns)
        command.upgrade(config, "head")
        assert snapshot(engine, old_columns) == before
        with Session(engine) as session:
            for token, _, _ in deliveries:
                attempt = session.get(JobAttempt, token.attempt_id)
                assert (
                    attempt.execution_stopped_at is None
                    and attempt.execution_exit_code is None
                )
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        old, running = deliveries
        assert publish_result(engine, old[0], invocation_id=old[1], store=store)
        assert not reservations.record_execution_stopped(
            engine, old[0], invocation_id=old[1], exit_code=0
        )
        assert not publish_result(
            engine, running[0], invocation_id=running[1], store=store
        )
        assert snapshot(engine, old_columns) == before
        assert reservations.record_execution_stopped(
            engine, running[0], invocation_id=running[1], exit_code=0
        )
        assert publish_result(engine, running[0], invocation_id=running[1], store=store)
        published = snapshot(engine, old_columns)
        command.downgrade(config, PREVIOUS_REVISION)
        assert snapshot(engine, old_columns) == published
        assert not STOP_COLUMNS & {
            column["name"] for column in inspect(engine).get_columns("job_attempts")
        }
        assert any(
            item["column_names"] == ["invocation_id"]
            for item in inspect(engine).get_unique_constraints("job_attempts")
        )
        command.upgrade(config, "head")
        assert snapshot(engine, old_columns) == published
        for token, invocation_id, identity in deliveries:
            assert publish_result(
                engine, token, invocation_id=invocation_id, store=store
            )
            assert len(store.verify_bundle(identity).outputs) == 7
        with Session(engine) as session:
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
            for token, _, _ in deliveries:
                attempt = session.get(JobAttempt, token.attempt_id)
                assert (
                    attempt.execution_stopped_at is None
                    and attempt.execution_exit_code is None
                )
    finally:
        engine.dispose()
