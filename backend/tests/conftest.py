import pytest
import app.jobs as jobs
import app.uploads as uploads
from app.main import create_app
from fastapi.testclient import TestClient
from types import SimpleNamespace

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