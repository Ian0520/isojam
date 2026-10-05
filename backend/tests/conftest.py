from datetime import timedelta
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import sessionmaker

from app.database import enable_sqlite_foreign_keys
from app.db_models import Base
from app.main import create_app
from app.security import create_access_token
from tests.factories import create_test_user, make_wav_bytes


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
            output_id="guitar", output_path=store_dir / "test_guitar.wav"
        )

        manifest = SimpleNamespace(outputs=[fake_guitar_output])
        return manifest


@pytest.fixture
def fake_model_session():
    return FakeModelSession()


@pytest.fixture
def client(
    fake_model_session,
    test_session_factory,
    jwt_secret_key,
):
    def fake_factory():
        return fake_model_session

    test_app = create_app(
        model_session_factory=fake_factory,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
    )

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


@pytest.fixture
def jwt_secret_key():
    return "test-jwt-secret-key-that-is-at-least-32-bytes"


@pytest.fixture
def auth_headers(test_session_factory, jwt_secret_key):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()
    access_token = create_access_token(
        user_id=user_id,
        secret_key=jwt_secret_key,
        expires_delta=timedelta(minutes=5),
    )
    return {"Authorization": f"Bearer {access_token}"}


@pytest.fixture
def wav_bytes():
    return make_wav_bytes()


@pytest.fixture(params=["models", "migration"])
def reservation_engine(request, tmp_path, monkeypatch):
    path = tmp_path / "reservations.db"
    url = URL.create("sqlite", database=str(path))
    # Spawned processes import app.database afresh; keep that engine's configured
    # path inside this test too, even though reservations use the explicit engine.
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    if request.param == "migration":
        monkeypatch.setenv("ALEMBIC_DATABASE_URL", str(url))
        command.upgrade(Config("alembic.ini"), "head")
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    if request.param == "models":
        Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()
