import subprocess
import sys
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.routers.jobs as job_routes
import app.storage as storage
from app.db_models import Job, JobOutput, Upload
from app.main import create_app
from tests.factories import create_test_job


@pytest.mark.parametrize("mode", ["disabled", "queued"])
def test_nonlocal_processing_skips_model_factory_and_serves_health(
    jwt_secret_key, mode
):
    model_factory = Mock(
        side_effect=AssertionError("Nonlocal processing must not load a model")
    )
    app = create_app(
        model_session_factory=model_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode=mode,
    )
    with TestClient(app) as client:
        assert app.state.model_session is None
        assert app.state.processing_mode == mode
        assert client.get("/health").status_code == 200
    model_factory.assert_not_called()


@pytest.mark.parametrize("mode", ["disabled", "queued"])
def test_processing_mode_can_be_selected_from_environment(
    jwt_secret_key, monkeypatch, mode
):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", mode)
    model_factory = Mock(
        side_effect=AssertionError("Nonlocal processing must not load a model")
    )
    app = create_app(model_session_factory=model_factory, jwt_secret_key=jwt_secret_key)
    with TestClient(app):
        assert app.state.model_session is None
    model_factory.assert_not_called()


def test_injected_local_mode_overrides_environment_and_closes_model(
    fake_model_session, jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", "invalid")
    app = create_app(
        model_session_factory=lambda: fake_model_session,
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
    )
    with TestClient(app):
        assert app.state.model_session is fake_model_session
        assert not fake_model_session.closed
    assert fake_model_session.closed


def test_invalid_processing_mode_fails_before_model_loading(
    jwt_secret_key, monkeypatch
):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", "invalid")
    model_factory = Mock(
        side_effect=AssertionError("Invalid configuration must not load a model")
    )
    app = create_app(model_session_factory=model_factory, jwt_secret_key=jwt_secret_key)
    with pytest.raises(RuntimeError, match="ISOJAM_PROCESSING_MODE"):
        with TestClient(app):
            pass
    model_factory.assert_not_called()


@pytest.mark.parametrize("mode", ["", "invalid", "LOCAL", False])
def test_invalid_injected_processing_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="processing_mode"):
        create_app(processing_mode=mode)


def test_disabled_processing_rejects_jobs_without_persistence_or_dispatch_and_keeps_existing_results_available(
    test_session_factory,
    jwt_secret_key,
    auth_headers,
    wav_bytes,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path / "uploads")
    process_job = Mock()
    monkeypatch.setattr(job_routes, "process_job", process_job)
    app = create_app(
        db_session_factory=test_session_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="disabled",
    )
    with TestClient(app) as client:
        upload_response = client.post(
            "/uploads",
            files={"audio_file": ("song.wav", wav_bytes, "audio/wav")},
            headers=auth_headers,
        )
        assert upload_response.status_code == 200
        upload_id = upload_response.json()["id"]
        response = client.post(
            "/jobs", json={"upload_id": upload_id}, headers=auth_headers
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "Audio processing is unavailable"
        process_job.assert_not_called()
        with test_session_factory() as session:
            assert session.scalar(select(func.count()).select_from(Job)) == 0
            upload = session.scalar(select(Upload))
            completed_job = create_test_job(session, upload, "completed")
            job_id = completed_job.id
            output_path = tmp_path / "guitar.wav"
            output_path.write_bytes(wav_bytes)
            session.add(JobOutput(job_id=job_id, stem="guitar", path=str(output_path)))
            session.commit()
        status_response = client.get(f"/jobs/{job_id}", headers=auth_headers)
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "completed"
        download = client.get(f"/jobs/{job_id}/outputs/guitar", headers=auth_headers)
        assert download.status_code == 200
        assert download.content == wav_bytes
        assert (
            client.post(
                "/jobs", json={"upload_id": str(uuid4())}, headers=auth_headers
            ).status_code
            == 404
        )
        assert client.post("/jobs", json={"upload_id": upload_id}).status_code == 401


@pytest.mark.parametrize("mode", ["disabled", "queued"])
def test_cpu_api_runs_without_importing_inference_packages(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", mode)
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    script = """
import importlib.abc
import sys

class BlockInferenceImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'bs_roformer', 'torch'}:
            raise AssertionError('API attempted to import inference package: ' + fullname)

sys.meta_path.insert(0, BlockInferenceImports())
from fastapi.testclient import TestClient
from app.db_models import Base
from app.database import engine
from app.main import create_app

Base.metadata.create_all(engine)
app = create_app(jwt_secret_key='test-secret-at-least-32-bytes-long', max_upload_bytes=1024, max_audio_duration_seconds=60, max_unfinished_jobs_per_user=2)
with TestClient(app) as client:
    assert client.get('/health').status_code == 200
    credentials = {'email': 'cpu@example.com', 'password': 'correct-horse-battery-staple'}
    assert client.post('/register', json=credentials).status_code == 201
    assert client.post('/login', json=credentials).status_code == 200
assert 'bs_roformer' not in sys.modules
assert 'torch' not in sys.modules
print('CPU API boundary passed')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CPU API boundary passed" in result.stdout


def test_local_processing_reports_missing_inference_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(tmp_path / "metadata.db"))
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(tmp_path / "audio"))
    script = """
import importlib.abc
import sys

class MissingInference(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'bs_roformer':
            raise ModuleNotFoundError("No module named 'bs_roformer'", name='bs_roformer')

sys.meta_path.insert(0, MissingInference())
from fastapi.testclient import TestClient
from app.main import create_app

app = create_app(jwt_secret_key='test-secret-at-least-32-bytes-long', processing_mode='local', max_upload_bytes=1024, max_audio_duration_seconds=60, max_unfinished_jobs_per_user=2)
try:
    with TestClient(app):
        raise AssertionError('Local processing must not start without inference dependencies')
except RuntimeError as error:
    assert 'inference' in str(error)
else:
    raise AssertionError('Missing inference dependencies must be reported clearly')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_local_model_failure_prevents_startup_instead_of_disabling_processing(
    jwt_secret_key,
):
    model_factory = Mock(side_effect=RuntimeError("CUDA startup failed"))
    app = create_app(
        model_session_factory=model_factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="local",
    )
    with pytest.raises(RuntimeError, match="CUDA startup failed"):
        with TestClient(app):
            pass
    model_factory.assert_called_once()
