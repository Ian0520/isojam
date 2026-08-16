from sqlalchemy import create_engine, inspect
from app.database import initialize_database

def test_initialize_database_creates_tables(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    test_engine = create_engine(tmp_url)

    table_names = inspect(test_engine).get_table_names()
    assert table_names == []

    initialize_database(test_engine)
    table_names = inspect(test_engine).get_table_names()
    assert set(table_names) == {
        "uploads",
        "jobs",
        "job_outputs",
    }