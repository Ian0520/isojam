import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import URL, create_engine, inspect

ALEMBIC_CONFIG = Path(__file__).resolve().parents[1] / "alembic.ini"


def run_python_module(*args, cwd):
    result = subprocess.run(
        [sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def run_migration(database_path, *args, cwd):
    # Refuse any connection outside this test's selected database, even if a
    # regression accidentally restores the source-checkout default.
    script = """
from pathlib import Path
import sys
from sqlalchemy import event
from sqlalchemy.engine import Engine
from alembic.config import main

@event.listens_for(Engine, "do_connect")
def check_database(dialect, connection_record, args, kwargs):
    assert dialect.name == "sqlite"
    assert Path(args[0]).resolve() == Path(sys.argv[1]).resolve(), "Unexpected migration database"

main(argv=sys.argv[2:])
"""
    return run_python_module(
        "-c", script, str(database_path), "-c", str(ALEMBIC_CONFIG), *args, cwd=cwd
    )


@pytest.mark.parametrize("directory_name", ["persistent metadata", "metadata ?#% disk"])
def test_migrations_and_restarted_application_use_configured_database(
    tmp_path,
    monkeypatch,
    directory_name,
):
    database_path = tmp_path / directory_name / "isojam.db"
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(database_path))
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)

    run_migration(database_path, "upgrade", "head", cwd=working_dir)
    assert database_path.is_file()
    engine = create_engine(URL.create("sqlite", database=str(database_path)))
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version",
        "users",
        "uploads",
        "jobs",
        "job_outputs",
        "job_attempts",
        "job_submission_receipts",
    }
    engine.dispose()

    script = """
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.database import DATABASE_PATH, SessionLocal, engine
from app.db_models import Upload, User
from app.main import create_app

assert DATABASE_PATH == Path(sys.argv[1])
assert engine.url.database == sys.argv[1]

class FakeModel:
    def close(self):
        pass

app = create_app(
    model_session_factory=FakeModel,
    jwt_secret_key="test-database-secret-at-least-32-bytes",
    max_upload_bytes=1024,
    max_audio_duration_seconds=60,
    max_unfinished_jobs_per_user=2,
)
credentials = {"email": "database@example.com", "password": "correct-horse-battery-staple"}
with TestClient(app) as client:
    if sys.argv[2] == "register":
        registered = client.post("/register", json=credentials)
        assert registered.status_code == 201, registered.text
    login = client.post("/login", json=credentials)
    assert login.status_code == 200, login.text
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(User)) == 1
    assert session.scalar(text("PRAGMA foreign_keys")) == 1
    session.add(Upload(user_id=uuid4(), original_filename="missing.wav", stored_filename="missing.wav"))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
    else:
        raise AssertionError("Configured database must enforce foreign keys")
print("Configured database flow passed")
"""
    for phase in ("register", "login-after-restart"):
        result = run_python_module(
            "-c", script, str(database_path), phase, cwd=working_dir
        )
        assert "Configured database flow passed" in result.stdout
    assert not (working_dir / "data").exists()


def test_alembic_explicit_url_overrides_application_path_without_initializing_it(
    tmp_path, monkeypatch
):
    application_path = tmp_path / "unused application database" / "isojam.db"
    migration_path = tmp_path / "migration % database" / "test.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(application_path))
    monkeypatch.setenv(
        "ALEMBIC_DATABASE_URL", str(URL.create("sqlite", database=str(migration_path)))
    )
    run_migration(migration_path, "upgrade", "head", cwd=tmp_path)
    assert migration_path.is_file()
    assert not application_path.parent.exists()
    engine = create_engine(URL.create("sqlite", database=str(migration_path)))
    assert "users" in inspect(engine).get_table_names()
    engine.dispose()


def test_initial_offline_migration_does_not_create_database(tmp_path, monkeypatch):
    database_path = tmp_path / "offline metadata" / "isojam.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(database_path))
    monkeypatch.delenv("ALEMBIC_DATABASE_URL", raising=False)
    result = run_migration(
        database_path, "upgrade", "cc4ab351693a", "--sql", cwd=tmp_path
    )
    assert "CREATE TABLE uploads" in result.stdout
    assert not database_path.parent.exists()
