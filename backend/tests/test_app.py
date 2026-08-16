from app.main import create_app

from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

def test_app_model_session_lifecycle(fake_model_session, test_engine):
    def fake_factory():
        return fake_model_session
    test_app = create_app(model_session_factory=fake_factory,
                          db_engine=test_engine,
                          )

    with TestClient(test_app) as client:
        assert test_app.state.model_session is fake_model_session
        assert not fake_model_session.closed
        
        response = client.get("/health")
        assert response.status_code == 200

    assert fake_model_session.closed

def test_app_initializes_database_on_startup(tmp_path, fake_model_session):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    test_engine = create_engine(tmp_url)

    test_session_factory = sessionmaker(test_engine)
    assert inspect(test_engine).get_table_names() == []
    def fake_factory():
        return fake_model_session
    test_app = create_app(
        model_session_factory=fake_factory,
        db_session_factory=test_session_factory,
        db_engine=test_engine,
    )
    with TestClient(test_app):
        table_names = inspect(test_engine).get_table_names()
        assert set(table_names) == {
            "uploads",
            "jobs",
            "job_outputs",
        }