from alembic import command
from alembic.config import Config
from sqlalchemy import URL, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from app.db_models import JobOutput, JobSubmissionReceipt, RegistrationInvitation
from app.invitations import issue_invitation
from tests.factories import create_test_job, create_test_upload, create_test_user

PREVIOUS_REVISION = "b5d8f3027a91"


def snapshot(engine):
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(f"SELECT * FROM {table} ORDER BY 1, 2")
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


def test_invitation_migration_preserves_accounts_jobs_receipts_and_audio(
    tmp_path, monkeypatch
):
    url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(url)
    recording = tmp_path / "guitar.wav"
    recording.write_bytes(b"existing-audio-bytes")
    try:
        with Session(engine) as session:
            user = create_test_user(session)
            upload = create_test_upload(session, user)
            job = create_test_job(session, upload, "completed")
            session.add(JobOutput(job_id=job.id, stem="guitar", path=str(recording)))
            session.add(
                JobSubmissionReceipt(
                    user_id=user.id,
                    key="old-key",
                    request_fingerprint="a" * 64,
                    job_id=job.id,
                )
            )
            session.commit()
        before = snapshot(engine)
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            assert session.scalars(select(RegistrationInvitation)).all() == []
            issue_invitation(session, "friend@example.com")
            session.commit()
        assert snapshot(engine) == before
        command.downgrade(config, PREVIOUS_REVISION)
        assert "registration_invitations" not in inspect(engine).get_table_names()
        assert snapshot(engine) == before
        assert recording.read_bytes() == b"existing-audio-bytes"
        command.upgrade(config, "head")
        assert snapshot(engine) == before
        with Session(engine) as session:
            assert session.scalars(select(RegistrationInvitation)).all() == []
            assert session.execute(text("PRAGMA foreign_key_check")).all() == []
    finally:
        engine.dispose()
