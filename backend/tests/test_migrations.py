from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_upgrade_head_creates_current_schema(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(db_url)

    table_names = inspect(engine).get_table_names()

    assert set(table_names) == {
        "uploads",
        "jobs",
        "job_outputs",
        "alembic_version",
    }

def test_downgrade_base_removes_schema(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path}/test.db"
    monkeypatch.setenv("ALEMBIC_DATABASE_URL", db_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    
    engine = create_engine(db_url)

    table_names = inspect(engine).get_table_names()
    assert set(table_names) == {
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
        