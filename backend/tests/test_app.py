from app.main import create_app
from fastapi.testclient import TestClient


def test_app_model_session_lifecycle(fake_model_session):
    def fake_factory():
        return fake_model_session
    test_app = create_app(model_session_factory=fake_factory)

    with TestClient(test_app) as client:
        assert test_app.state.model_session is fake_model_session
        assert not fake_model_session.closed
        response = client.get("/health")
        assert response.status_code == 200

    assert fake_model_session.closed