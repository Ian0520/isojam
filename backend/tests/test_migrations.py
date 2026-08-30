from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_upgrade_head_creates_current_schema(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    engine = create_engine(db_url)
    inspector = inspect(engine)

    table_names = inspector.get_table_names()
    assert set(table_names) == {
        "users",
        "uploads",
        "jobs",
        "job_outputs",
        "alembic_version",
    }

    upload_columns = {
        column["name"]: column
        for column in inspector.get_columns("uploads")
    }

    assert "user_id" in upload_columns
    assert not upload_columns["user_id"]["nullable"]

    upload_foreign_keys = inspector.get_foreign_keys("uploads")

    assert any(
        foreign_key["constrained_columns"] == ["user_id"]
        and foreign_key["referred_table"] == "users"
        and foreign_key["referred_columns"] == ["id"]
        for foreign_key in upload_foreign_keys
    )

def test_downgrade_base_removes_schema(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    
    engine = create_engine(db_url)

    table_names = inspect(engine).get_table_names()
    assert set(table_names) == {
        "users",
        "uploads",
        "jobs",
        "job_outputs",
        "alembic_version",
    }

    command.downgrade(config, "base")
    table_names = inspect(engine).get_table_names()
    assert "uploads" not in table_names
    assert "jobs" not in table_names
    assert "job_outputs" not in table_names
    assert "users" not in table_names


def test_downgrade_user_ownership_migration_restores_previous_schema(
    tmp_path,
    monkeypatch,
):
    db_url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)

    config = Config("alembic.ini")

    command.upgrade(config, "head")
    command.downgrade(config, "cc4ab351693a")

    engine = create_engine(db_url)
    inspector = inspect(engine)

    table_names = inspector.get_table_names()

    assert "users" not in table_names

    assert {
        "uploads",
        "jobs",
        "job_outputs",
        "alembic_version",
    }.issubset(table_names)

    upload_column_names = {
        column["name"]
        for column in inspector.get_columns("uploads")
    }

    assert "user_id" not in upload_column_names