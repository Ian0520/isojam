import pytest
import app.jobs as jobs
import app.uploads as uploads
from app.main import create_app
from fastapi.testclient import TestClient

class FakeModelSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

@pytest.fixture
def fake_model_session():
    return FakeModelSession()

@pytest.fixture(autouse=True)
def clear_registries():
    jobs.jobs.clear()
    uploads.uploads.clear()

@pytest.fixture
def client(fake_model_session):
    def fake_factory():
        return fake_model_session
    test_app = create_app(
        model_session_factory=fake_factory
    )

    with TestClient(test_app) as test_client:
        yield test_client