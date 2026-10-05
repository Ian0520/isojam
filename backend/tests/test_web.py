import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.parametrize("mode", ["local", "queued", "disabled"])
def test_browser_assets_and_runtime_info_in_each_mode(
    mode, fake_model_session, jwt_secret_key, test_session_factory
):
    app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode=mode,
        max_upload_bytes=1024,
        max_audio_duration_seconds=30,
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/ui-info").json() == {
            "processing_mode": mode,
            "max_upload_bytes": 1024,
            "max_audio_duration_seconds": 30,
        }
        assert (
            client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 401
        )
    assert fake_model_session.called is False
    assert fake_model_session.closed is (mode == "local")
