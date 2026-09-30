import subprocess
import sys
from base64 import b64encode


def test_configured_audio_storage_supports_upload_processing_and_download_from_another_directory(
    tmp_path,
    monkeypatch,
    wav_bytes,
):
    audio_dir = tmp_path / "persistent audio"
    working_dir = tmp_path / "working"
    working_dir.mkdir()
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(audio_dir))
    script = """
from base64 import b64decode
from pathlib import Path
from types import SimpleNamespace
import sys

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db_models import Base, JobOutput, Upload
from app.main import create_app

root = Path(sys.argv[1])
engine = create_engine(f"sqlite:///{sys.argv[2]}")
Base.metadata.create_all(engine)
session_factory = sessionmaker(engine)
payload = b64decode(sys.argv[3])

class FakeModel:
    def close(self):
        pass

    def infer(self, input_folder, *, store_dir):
        inputs = list(input_folder.glob("*.wav"))
        assert len(inputs) == 1
        assert inputs[0].resolve().parent == root / "uploads"
        assert inputs[0].read_bytes() == payload
        assert store_dir.parent == root / "outputs"
        output_path = store_dir / "guitar.wav"
        output_path.write_bytes(payload)
        return SimpleNamespace(outputs=[SimpleNamespace(output_id="guitar", output_path=output_path)])

app = create_app(
    model_session_factory=FakeModel,
    db_session_factory=session_factory,
    jwt_secret_key="test-secret-key-at-least-32-bytes-long",
    max_upload_bytes=1024 * 1024,
    max_audio_duration_seconds=60,
    max_unfinished_jobs_per_user=2,
)
with TestClient(app) as client:
    credentials = {"email": "storage@example.com", "password": "correct-horse-battery-staple"}
    assert client.post("/register", json=credentials).status_code == 201
    login = client.post("/login", json=credentials)
    assert login.status_code == 200
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    uploaded = client.post("/uploads", files={"audio_file": ("song.wav", payload, "audio/wav")}, headers=headers)
    assert uploaded.status_code == 200
    created = client.post("/jobs", json={"upload_id": uploaded.json()["id"]}, headers=headers)
    assert created.status_code == 200
    job_id = created.json()["id"]
    status = client.get(f"/jobs/{job_id}", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    download = client.get(status.json()["outputs"]["guitar"], headers=headers)
    assert download.status_code == 200
    assert download.content == payload
with session_factory() as session:
    upload = session.scalar(select(Upload))
    assert (root / "uploads" / upload.stored_filename).read_bytes() == payload
    output = session.scalar(select(JobOutput))
    assert Path(output.path) == root / "outputs" / job_id / "guitar.wav"
assert not (Path.cwd() / "data").exists()
print("Configured storage flow passed")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(audio_dir),
            str(tmp_path / "test.db"),
            b64encode(wav_bytes).decode(),
        ],
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configured storage flow passed" in result.stdout
