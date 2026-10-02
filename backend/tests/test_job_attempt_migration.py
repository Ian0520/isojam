from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import URL, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.database import enable_sqlite_foreign_keys
from app.db_models import Base, Job, JobAttempt, JobOutput
from app.main import create_app
from app.security import create_access_token
from tests.factories import make_wav_bytes

PREVIOUS_REVISION = "47d7085ef617"


def legacy_snapshot(engine):
    queries = {
        "users": "SELECT id, email, password_hash FROM users ORDER BY id",
        "uploads": "SELECT id, user_id, original_filename, stored_filename FROM uploads ORDER BY id",
        "jobs": "SELECT id, upload_id, status FROM jobs ORDER BY id",
        "job_outputs": "SELECT job_id, stem, path FROM job_outputs ORDER BY job_id, stem",
    }
    with engine.connect() as connection:
        return {
            name: connection.execute(text(query)).all()
            for name, query in queries.items()
        }


@pytest.fixture
def populated_previous_database(tmp_path, monkeypatch):
    database_url = URL.create("sqlite", database=str(tmp_path / "existing.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(database_url))
    config = Config("alembic.ini")
    command.upgrade(config, PREVIOUS_REVISION)
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    user_id, upload_id = uuid4(), uuid4()
    job_ids = {
        status: uuid4() for status in ("pending", "processing", "completed", "failed")
    }
    output_path = tmp_path / "old outputs" / "guitar.wav"
    output_path.parent.mkdir()
    output_bytes = make_wav_bytes()
    output_path.write_bytes(output_bytes)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) VALUES (:id, :email, :password_hash)"
            ),
            {
                "id": user_id.hex,
                "email": "migration@example.com",
                "password_hash": "existing-hash",
            },
        )
        connection.execute(
            text(
                "INSERT INTO uploads (id, user_id, original_filename, stored_filename) "
                "VALUES (:id, :user_id, :original_filename, :stored_filename)"
            ),
            {
                "id": upload_id.hex,
                "user_id": user_id.hex,
                "original_filename": "song.wav",
                "stored_filename": "stored-song.wav",
            },
        )
        connection.execute(
            text(
                "INSERT INTO jobs (id, upload_id, status) VALUES (:id, :upload_id, :status)"
            ),
            [
                {"id": job_id.hex, "upload_id": upload_id.hex, "status": status}
                for status, job_id in job_ids.items()
            ],
        )
        connection.execute(
            text(
                "INSERT INTO job_outputs (job_id, stem, path) VALUES (:job_id, :stem, :path)"
            ),
            {
                "job_id": job_ids["completed"].hex,
                "stem": "guitar",
                "path": str(output_path),
            },
        )
    try:
        yield SimpleNamespace(
            engine=engine,
            config=config,
            user_id=user_id,
            job_ids=job_ids,
            output_path=output_path,
            output_bytes=output_bytes,
            snapshot=legacy_snapshot(engine),
        )
    finally:
        engine.dispose()


def test_upgrade_preserves_existing_jobs_and_owned_downloads(
    populated_previous_database,
):
    existing = populated_previous_database
    before = datetime.now(UTC).replace(microsecond=0)
    command.upgrade(existing.config, "head")
    assert legacy_snapshot(existing.engine) == existing.snapshot
    with Session(existing.engine) as session:
        assert session.scalars(select(JobAttempt)).all() == []
        for status, job_id in existing.job_ids.items():
            job = session.get(Job, job_id)
            assert job.status == status
            assert job.execution_backend == "local"
            assert job.created_at.tzinfo is UTC
            assert before <= job.created_at <= datetime.now(UTC)
            assert job.updated_at == job.created_at
        output = session.get(JobOutput, (existing.job_ids["completed"], "guitar"))
        assert output.path == str(existing.output_path)
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []

    secret = "migration-test-signing-key-at-least-32-bytes"
    app = create_app(
        db_session_factory=sessionmaker(existing.engine),
        jwt_secret_key=secret,
        processing_mode="disabled",
    )
    token = create_access_token(
        user_id=existing.user_id,
        secret_key=secret,
        expires_delta=timedelta(minutes=5),
    )
    with TestClient(app) as client:
        response = client.get(
            f"/jobs/{existing.job_ids['completed']}/outputs/guitar",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.content == existing.output_bytes


def test_downgrade_restores_legacy_schema_and_preserves_records(
    populated_previous_database,
):
    existing = populated_previous_database
    command.upgrade(existing.config, "head")
    with Session(existing.engine) as session:
        session.add(JobAttempt(job_id=existing.job_ids["pending"], attempt_number=1))
        session.commit()
    command.downgrade(existing.config, PREVIOUS_REVISION)
    inspector = inspect(existing.engine)
    assert "job_attempts" not in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("jobs")} == {
        "id",
        "upload_id",
        "status",
    }
    assert legacy_snapshot(existing.engine) == existing.snapshot
    assert existing.output_path.read_bytes() == existing.output_bytes
    with existing.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_key_check")).all() == []
    command.upgrade(existing.config, "head")
    assert legacy_snapshot(existing.engine) == existing.snapshot


def test_migrated_schema_matches_current_models(tmp_path, monkeypatch):
    database_url = URL.create("sqlite", database=str(tmp_path / "schema.db"))
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(database_url))
    command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_server_default": True}
            )
            assert compare_metadata(context, Base.metadata) == []
    finally:
        engine.dispose()
