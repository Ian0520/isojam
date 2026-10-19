"""Opt-in live HTTP/import check using disposable CPU containers and storage."""

import argparse
import json
import secrets
import time
import uuid

from smoke_container import (
    LABEL,
    docker,
    owns_resource,
    post_json,
    request,
    wait_for_health,
)

INSPECT_UPLOAD = """
import hashlib, json, sys, uuid
from pathlib import Path
import soundfile as sf
from app.database import SessionLocal
from app.db_models import Upload
with SessionLocal() as session:
    upload = session.get(Upload, uuid.UUID(sys.argv[1]))
    path = Path('/var/lib/isojam/audio/uploads') / upload.stored_filename
    info = sf.info(path)
    assert info.format == 'WAV' and info.subtype == 'FLOAT'
    assert info.samplerate == 44100 and info.channels in {1, 2}
    assert info.duration <= 600
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    assert list(Path('/var/lib/isojam/audio/imports').iterdir()) == [
        Path('/var/lib/isojam/audio/imports/worker.lock')]
    print(json.dumps(dict(frames=info.frames, bytes=path.stat().st_size, sha256=digest)))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", default="isojam-api:local-stack")
    parser.add_argument("--import-image", default="isojam-youtube:local-stack")
    parser.add_argument("--video-url", default="https://youtu.be/YE7VzlLtp-4")
    args = parser.parse_args()
    marker = "isojam-youtube-check-" + uuid.uuid4().hex[:12]
    volume, api, worker = marker + "-data", marker + "-api", marker + "-worker"
    label = ["--label", f"{LABEL}={marker}"]
    mount = ["--mount", f"type=volume,src={volume},dst=/var/lib/isojam"]
    environment = [
        "-e",
        "ISOJAM_DATABASE_PATH=/var/lib/isojam/metadata/isojam.db",
        "-e",
        "ISOJAM_AUDIO_STORAGE_DIR=/var/lib/isojam/audio",
    ]

    def api_port():
        # Docker may allocate a different ephemeral host port after a restart.
        bindings = json.loads(
            docker("inspect", "--format", "{{json .NetworkSettings.Ports}}", api).stdout
        )
        port = int(bindings["8000/tcp"][0]["HostPort"])
        wait_for_health(api, port)
        return port

    def snapshot(upload_id):
        return json.loads(
            docker("exec", api, "python", "-c", INSPECT_UPLOAD, upload_id).stdout
        )

    try:
        docker("volume", "create", *label, volume)
        docker(
            "run",
            "--rm",
            *environment,
            *mount,
            args.api_image,
            "python",
            "-m",
            "alembic",
            "upgrade",
            "head",
        )
        docker(
            "run",
            "-d",
            "--init",
            "--name",
            api,
            *label,
            *environment,
            *mount,
            "-e",
            "ISOJAM_PROCESSING_MODE=queued",
            "-e",
            "ISOJAM_REGISTRATION_MODE=open",
            "-e",
            "ISOJAM_YOUTUBE_IMPORTS_ENABLED=1",
            "-e",
            "ISOJAM_JWT_SECRET_KEY=" + secrets.token_hex(32),
            "-p",
            "127.0.0.1::8000",
            args.api_image,
        )
        port = api_port()
        credentials = {
            "email": "import-check@example.com",
            "password": "correct-horse-battery-staple",
        }
        assert post_json(port, "/register", credentials)[0] == 201
        status, token = post_json(port, "/login", credentials)
        assert status == 200
        auth = {"Authorization": "Bearer " + token["access_token"]}
        headers = auth | {"Idempotency-Key": "live-import"}
        body = {"url": args.video_url}
        began = time.monotonic()
        status, item = post_json(port, "/imports", body, headers)
        latency = time.monotonic() - began
        assert status == 202 and item["status"] == "pending", (status, item)
        docker("restart", api)
        port = api_port()
        status, replay = post_json(port, "/imports", body, headers)
        assert status == 202 and replay["id"] == item["id"]
        assert replay["status"] == "pending"
        docker(
            "run",
            "-d",
            "--init",
            "--name",
            worker,
            *label,
            *environment,
            *mount,
            args.import_image,
            "python",
            "-m",
            "app.import_worker",
            "serve",
        )
        deadline = time.monotonic() + 165
        while time.monotonic() < deadline:
            status, ready = request(port, "GET", "/imports/" + item["id"], headers=auth)
            assert status == 200
            if ready["status"] in {"ready", "failed"}:
                break
            time.sleep(0.5)
        assert ready["status"] == "ready", ready
        before = snapshot(ready["upload_id"])
        status, job = post_json(
            port,
            "/jobs",
            {"upload_id": ready["upload_id"]},
            auth | {"Idempotency-Key": "separate-import"},
        )
        assert status == 200 and job["status"] == "pending"
        docker("stop", "--time", "180", worker)
        assert (
            docker("inspect", "--format", "{{.State.ExitCode}}", worker).stdout.strip()
            == "0"
        )
        docker("restart", api)
        port = api_port()
        status, replay = post_json(port, "/imports", body, headers)
        assert status == 202 and replay["upload_id"] == ready["upload_id"]
        assert snapshot(ready["upload_id"]) == before
        print(
            json.dumps(
                {
                    "status": "passed",
                    "acceptance_seconds": round(latency, 3),
                    "import_status": ready["status"],
                    "separation_status": job["status"],
                    "wav": before,
                }
            ),
            flush=True,
        )
    except BaseException:
        for name in (api, worker):
            if owns_resource("container", name, marker):
                result = docker("logs", "--tail", "30", name, check=False)
                print(name, result.stdout, result.stderr)
        raise
    finally:
        for name in (worker, api):
            if owns_resource("container", name, marker):
                docker("rm", "-f", name, check=False)
        if owns_resource("volume", volume, marker):
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
