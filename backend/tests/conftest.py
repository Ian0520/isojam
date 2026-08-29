from app.main import create_app
from app.db_models import Base
from app.database import enable_sqlite_foreign_keys, get_db

import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

class FakeModelSession:
    def __init__(self):
        self.closed = False
        self.called = False

    def close(self):
        self.closed = True

    def infer(self, input_folder, *, store_dir):
        self.called = True
        self.input_folder = input_folder
        self.store_dir = store_dir

        fake_guitar_output = SimpleNamespace(
            output_id="guitar",
            output_path=store_dir / "test_guitar.wav"
        )

        manifest = SimpleNamespace(outputs=[fake_guitar_output])
        return manifest


@pytest.fixture
def fake_model_session():
    return FakeModelSession()

@pytest.fixture
def client(fake_model_session, test_session_factory):
    def override_get_db():
        with test_session_factory() as session:
            yield session

    def fake_factory():
        return fake_model_session
    test_app = create_app(
        model_session_factory=fake_factory,
        db_session_factory=test_session_factory,
    )

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app) as test_client:
        yield test_client

@pytest.fixture
def test_engine(tmp_path):
    tmp_url = f"sqlite:///{tmp_path}/test.db"
    tmp_engine = create_engine(tmp_url)
    enable_sqlite_foreign_keys(tmp_engine)
    Base.metadata.create_all(tmp_engine)

    return tmp_engine

@pytest.fixture
def test_session_factory(test_engine):
    return sessionmaker(test_engine)
