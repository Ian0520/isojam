from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput, JobSubmissionReceipt
from app.fake_worker import create_fake_workspace
from app.publication import publish_result
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore, ResultIdentity
from tests.factories import (
    create_test_job,
    create_test_upload,
    create_test_user,
    make_wav_bytes,
)

PREVIOUS_REVISION = "f3b8d2106a74"
PREVIOUS_ATTEMPT_COLUMNS = (
    "id, job_id, attempt_number, phase, created_at, updated_at, started_at, finished_at, "
    "dispatcher_id, dispatcher_generation, reservation_expires_at, "
    "execution_authorization_expires_at, invocation_id, last_heartbeat_at"
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


def test_publication_migration_preserves_data_and_does_not_invent_selected_manifest(
    tmp_path, monkeypatch, clock
):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    output_path = tmp_path / "existing-guitar.wav"
    output_bytes = make_wav_bytes()
    output_path.write_bytes(output_bytes)
    legacy_id = uuid4()
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            session.add(
                Job(upload_id=upload.id, execution_backend="queued", status="pending")
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
            session.execute(
                text(
                    "INSERT INTO job_attempts (id, job_id, attempt_number, phase) VALUES (:id, :job, 1, 'succeeded')"
                ),
                {"id": legacy_id.hex, "job": completed.id.hex},
            )
            session.commit()
        token = reservations.reserve_next_job(engine, dispatcher_id=uuid4())
        assert reservations.begin_submission(engine, token)
        invocation_id = uuid4()
        assert reservations.authorize_execution(
            engine, token, invocation_id=invocation_id
        )
        # Seed the old schema without invoking current code that uses new columns.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE job_attempts SET last_heartbeat_at = started_at WHERE id = :id"
                ),
                {"id": token.attempt_id.hex},
            )
        before = snapshot(engine)
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            for identifier in (legacy_id, token.attempt_id):
                attempt = session.get(JobAttempt, identifier)
                assert (
                    attempt.result_manifest_key is None
                    and attempt.result_manifest_sha256 is None
                )
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        create_fake_workspace(workspace)
        store = LocalResultStore(tmp_path / "results")
        identity = ResultIdentity(
            job_id=token.job_id,
            attempt_id=token.attempt_id,
            invocation_id=invocation_id,
        )
        bundle = store.write_bundle(identity, workspace)
        assert reservations.record_execution_stopped(
            engine,
            token,
            invocation_id=invocation_id,
            exit_code=0,
        )
        assert publish_result(engine, token, invocation_id=invocation_id, store=store)
        after_publication = snapshot(engine)
        command.downgrade(config, PREVIOUS_REVISION)
        assert snapshot(engine) == after_publication
        assert not {"result_manifest_key", "result_manifest_sha256"} & {
            column["name"] for column in inspect(engine).get_columns("job_attempts")
        }
        assert any(
            item["column_names"] == ["invocation_id"]
            for item in inspect(engine).get_unique_constraints("job_attempts")
        )
        command.upgrade(config, "head")
        assert snapshot(engine) == after_publication
        with Session(engine) as session:
            assert session.get(Job, token.job_id).status == "completed"
            attempt = session.get(JobAttempt, token.attempt_id)
            assert attempt.phase == "succeeded" and attempt.result_manifest_key is None
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        # Downgrade removed the evidence needed for idempotent acknowledgement;
        # re-upgrade must neither infer it nor overwrite existing output records.
        with pytest.raises(reservations.PublicationConflictError):
            publish_result(engine, token, invocation_id=invocation_id, store=store)
        assert store.verify_bundle(identity) == bundle
        assert output_path.read_bytes() == output_bytes
        with engine.connect() as connection:
            paths = (
                connection.exec_driver_sql("SELECT path FROM job_outputs")
                .scalars()
                .all()
            )
        assert len(paths) == 8 and all(Path(path).is_file() for path in paths)
    finally:
        engine.dispose()
