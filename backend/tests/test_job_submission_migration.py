from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import JobOutput, JobSubmissionReceipt
from app.repositories import job_submissions
from tests.factories import create_test_job, create_test_upload, create_test_user


def snapshot(engine):
    tables = ("users", "uploads", "jobs", "job_attempts", "job_outputs")
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(
                    "SELECT id, job_id, attempt_number, phase, created_at, updated_at, "
                    "started_at, finished_at FROM job_attempts ORDER BY 1, 2"
                    if table == "job_attempts"
                    else f"SELECT * FROM {table} ORDER BY 1, 2"
                )
            ).all()
            for table in tables
        }


def test_receipt_upgrade_and_downgrade_preserve_existing_records(tmp_path, monkeypatch):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, "b6e89a52d107")
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    output_path = tmp_path / "old-guitar.wav"
    output_path.write_bytes(b"existing-output-bytes")
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            job = create_test_job(session, upload, "completed")
            session.execute(
                text(
                    "INSERT INTO job_attempts (id, job_id, attempt_number, phase) "
                    "VALUES (:id, :job_id, 1, 'succeeded')"
                ),
                {"id": uuid4().hex, "job_id": job.id.hex},
            )
            session.add(JobOutput(job_id=job.id, stem="guitar", path=str(output_path)))
            user_id, upload_id = user.id, upload.id
            session.commit()
        before = snapshot(engine)
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            assert session.scalars(select(JobSubmissionReceipt)).all() == []
            submission = job_submissions.create_submission(
                session, upload_id, user_id, key="request-1", max_unfinished_jobs=2
            )
            assert submission.created
            session.commit()
        with Session(engine) as session:
            receipt = session.get(JobSubmissionReceipt, (user_id, "request-1"))
            assert (
                job_submissions.get_replayed_job(
                    session, user_id, upload_id, "request-1"
                ).id
                == receipt.job_id
            )
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
        with_receipt = snapshot(engine)
        command.downgrade(config, "b6e89a52d107")
        assert "job_submission_receipts" not in inspect(engine).get_table_names()
        assert snapshot(engine) == with_receipt
        assert output_path.read_bytes() == b"existing-output-bytes"
        command.upgrade(config, "head")
        assert snapshot(engine) == with_receipt
        with Session(engine) as session:
            assert session.scalars(select(JobSubmissionReceipt)).all() == []
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
