import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.mark.parametrize("mode", ["local", "queued", "disabled"])
@pytest.mark.parametrize("registration_mode", ["open", "closed", "invite"])
def test_browser_assets_and_runtime_info_in_each_mode(
    mode, registration_mode, fake_model_session, jwt_secret_key, test_session_factory
):
    app = create_app(
        model_session_factory=lambda: fake_model_session,
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode=mode,
        registration_mode=registration_mode,
        max_upload_bytes=1024,
        max_audio_duration_seconds=30,
        max_converted_audio_bytes=2048,
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert client.get("/static/app.js").status_code == 200
        auth_script = client.get("/static/auth.js")
        assert auth_script.status_code == 200
        assert "javascript" in auth_script.headers["content-type"]
        history_script = client.get("/static/history.js")
        assert history_script.status_code == 200
        assert "javascript" in history_script.headers["content-type"]
        assert client.get("/static/uploads.js").status_code == 200
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/ui-info").json() == {
            "processing_mode": mode,
            "registration_mode": registration_mode,
            "max_upload_bytes": 1024,
            "max_audio_duration_seconds": 30,
            "max_converted_audio_bytes": 2048,
            "audio_upload_types": {
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".m4a": "audio/mp4",
                ".aac": "audio/aac",
                ".flac": "audio/flac",
            },
        }
        assert (
            client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 401
        )
    assert fake_model_session.called is False
    assert fake_model_session.closed is (mode == "local")


@pytest.mark.parametrize(
    "asset", ["app.js", "auth.js", "history.js", "uploads.js", "styles.css"]
)
def test_browser_assets_require_revalidation_including_not_modified(client, asset):
    # The new URL also bypasses old entries cached before this policy existed.
    url = f"/static/{asset}?v=workspace"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"

    cached = client.get(url, headers={"If-None-Match": response.headers["etag"]})
    assert cached.status_code == 304
    assert cached.headers["cache-control"] == "no-cache"
    assert cached.content == b""
