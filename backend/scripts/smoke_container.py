"""Exercise the built API image using a disposable named volume and real HTTP."""

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import time
import uuid
import wave
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LABEL = "io.isojam.smoke"


def docker(*args, env=None, check=True):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, env=env
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def owns_resource(kind, name, marker):
    template = '{{index .Labels "' + LABEL + '"}}'
    if kind == "container":
        template = '{{index .Config.Labels "' + LABEL + '"}}'
    result = docker(kind, "inspect", "--format", template, name, check=False)
    return result.returncode == 0 and result.stdout.strip() == marker


def request(port, method, path, data=None, headers=None):
    req = Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=headers or {},
        method=method,
    )
    try:
        with urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def post_json(port, path, payload, headers=None):
    return request(
        port,
        "POST",
        path,
        json.dumps(payload).encode(),
        {"Content-Type": "application/json", **(headers or {})},
    )


def wait_for_health(name, port):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        running = docker("inspect", "--format", "{{.State.Running}}", name)
        if running.stdout.strip() != "true":
            raise RuntimeError("API container exited before becoming ready")
        try:
            status, body = request(port, "GET", "/health")
            if status == 200 and body == {"status": "ok"}:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError("API container did not become ready within 30 seconds")


def make_wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\0\0" * 441)
    return buffer.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="isojam-api:local")
    parser.add_argument(
        "--processing-mode", choices=["disabled", "queued"], default="disabled"
    )
    parser.add_argument("--dispatch-fake-worker", action="store_true")
    parser.add_argument(
        "--recover-publication",
        action="store_true",
        help="Interrupt publication and recover using a fresh dispatcher container",
    )
    args = parser.parse_args()
    if args.dispatch_fake_worker and args.processing_mode != "queued":
        parser.error("--dispatch-fake-worker requires --processing-mode queued")
    if args.recover_publication and not args.dispatch_fake_worker:
        parser.error("--recover-publication requires --dispatch-fake-worker")
    if shutil.which("docker") is None:
        parser.error("Docker is unavailable. Start Docker and enable WSL integration.")
    docker("version", "--format", "{{.Server.Version}}")
    platform = docker(
        "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", args.image
    )
    if platform.stdout.strip() != "linux/amd64":
        parser.error("Build the image for linux/amd64 using the documented CPU locks.")

    marker = uuid.uuid4().hex
    volume = "isojam-smoke-" + marker
    name = "isojam-smoke-api-" + marker
    env = os.environ.copy()
    # Pass the temporary secret by environment name, not as a command argument.
    env["ISOJAM_JWT_SECRET_KEY"] = secrets.token_hex(32)
    common = [
        "--pull",
        "never",
        "--label",
        f"{LABEL}={marker}",
        "--env",
        "ISOJAM_JWT_SECRET_KEY",
        "--env",
        f"ISOJAM_PROCESSING_MODE={args.processing_mode}",
        "--mount",
        f"type=volume,src={volume},dst=/var/lib/isojam",
    ]
    try:
        docker("volume", "create", "--label", f"{LABEL}={marker}", volume)
        assert owns_resource("volume", volume, marker)
        boundary_check = r"""
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re

assert os.getuid() == 10001
assert os.environ['ISOJAM_PROCESSING_MODE'] in {'disabled', 'queued'}
assert os.environ['ISOJAM_DATABASE_PATH'] == '/var/lib/isojam/metadata/isojam.db'
assert os.environ['ISOJAM_AUDIO_STORAGE_DIR'] == '/var/lib/isojam/audio'
for package in ['torch', 'bs_roformer', 'pytest', 'httpx2', 'ruff']:
    assert importlib.util.find_spec(package) is None, package
for name, expected in re.findall(r'^([a-zA-Z0-9_-]+)==([^\s\\]+)', Path('requirements-api.txt').read_text(), re.M):
    assert importlib.metadata.version(name) == expected, name
for excluded in ['tests', '.venv', '.env', '.env.container', 'data']:
    assert not Path('/opt/isojam', excluded).exists(), excluded
assert not os.access('/opt/isojam/app/main.py', os.W_OK)
assert os.access('/var/lib/isojam/metadata', os.W_OK)
assert os.access('/var/lib/isojam/audio', os.W_OK)
print('Non-root runtime, pinned dependencies, and volume permissions passed')
"""
        result = docker(
            "run", "--rm", *common, args.image, "python", "-c", boundary_check, env=env
        )
        print(result.stdout.strip(), flush=True)
        docker(
            "run", "--rm", *common, args.image, "python", "-m", "pip", "check", env=env
        )
        docker(
            "run",
            "--rm",
            *common,
            args.image,
            "python",
            "-m",
            "alembic",
            "upgrade",
            "head",
            env=env,
        )
        print("Migrations passed on disposable named volume", flush=True)

        credentials = {
            "email": "container@example.com",
            "password": "correct-horse-battery-staple",
        }
        payload = make_wav()
        upload_id = None
        job_id = None
        for phase in ("first-start", "replacement"):
            docker(
                "run",
                "--detach",
                "--name",
                name,
                *common,
                "--publish",
                "127.0.0.1::8000",
                args.image,
                env=env,
            )
            ports = json.loads(
                docker(
                    "inspect", "--format", "{{json .NetworkSettings.Ports}}", name
                ).stdout
            )
            binding = ports["8000/tcp"][0]
            assert binding["HostIp"] == "127.0.0.1"
            port = int(binding["HostPort"])
            wait_for_health(name, port)
            if phase == "first-start":
                status, _ = post_json(port, "/register", credentials)
                assert status == 201, f"Registration returned {status}"
            status, login = post_json(port, "/login", credentials)
            assert status == 200, f"Login returned {status}"
            headers = {"Authorization": "Bearer " + login["access_token"]}
            if phase == "first-start":
                boundary = "isojam-smoke-boundary"
                body = (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="song.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
                    + payload
                    + f"\r\n--{boundary}--\r\n".encode()
                )
                status, upload = request(
                    port,
                    "POST",
                    "/uploads",
                    body,
                    {
                        "Content-Type": "multipart/form-data; boundary=" + boundary,
                        **headers,
                    },
                )
                assert status == 200, f"Upload returned {status}"
                upload_id = upload["id"]
            status, job = post_json(
                port,
                "/jobs",
                {"upload_id": upload_id},
                {**headers, "Idempotency-Key": "container-request"},
            )
            if args.processing_mode == "disabled":
                assert status == 503, f"Disabled processing returned {status}"
            else:
                assert status == 200, f"Queued submission returned {status}"
                assert job["status"] == "pending" and job["outputs"] == {}
                assert job["upload_id"] == upload_id
                if job_id is None:
                    job_id = job["id"]
                assert job["id"] == job_id, "Replay created a different job"
                status, current = request(
                    port, "GET", f"/jobs/{job_id}", headers=headers
                )
                assert status == 200 and current == job
                status, _ = request(
                    port, "GET", f"/jobs/{job_id}/outputs/guitar", headers=headers
                )
                assert status == 409, "Pending outputs must remain unavailable"
            status, _ = post_json(port, "/jobs", {"upload_id": upload_id})
            assert status == 401, f"Unauthenticated job submission returned {status}"
            print(
                f"{phase}: health, authentication, and {args.processing_mode} submission passed",
                flush=True,
            )
            docker("stop", "--time", "10", name)
            state = json.loads(
                docker("inspect", "--format", "{{json .State}}", name).stdout
            )
            assert state["ExitCode"] == 0, "API did not shut down cleanly"
            assert owns_resource("container", name, marker)
            docker("rm", name)

        persistence_check = """
from pathlib import Path
import hashlib
import sqlite3
import sys

with sqlite3.connect('/var/lib/isojam/metadata/isojam.db') as connection:
    assert connection.execute('select count(*) from users').fetchone()[0] == 1
    queued = sys.argv[3] == 'queued'
    assert connection.execute('select count(*) from jobs').fetchone()[0] == int(queued)
    assert connection.execute('select count(*) from job_submission_receipts').fetchone()[0] == int(queued)
    assert connection.execute('select count(*) from job_attempts').fetchone()[0] == 0
    assert connection.execute('select count(*) from job_outputs').fetchone()[0] == 0
    if queued:
        job_id = sys.argv[4].replace('-', '')
        assert connection.execute('select status, execution_backend from jobs where id=?', (job_id,)).fetchone() == ('pending', 'queued')
        assert connection.execute('select job_id from job_submission_receipts').fetchone()[0] == job_id
    rows = connection.execute('select id, stored_filename from uploads').fetchall()
    assert len(rows) == 1 and rows[0][0] == sys.argv[1].replace('-', '')
    stored = rows[0][1]
path = Path('/var/lib/isojam/audio/uploads') / stored
assert hashlib.sha256(path.read_bytes()).hexdigest() == sys.argv[2]
print('Account, upload, job/receipt state, and exact WAV bytes survived container replacement')
"""
        result = docker(
            "run",
            "--rm",
            *common,
            args.image,
            "python",
            "-c",
            persistence_check,
            upload_id,
            hashlib.sha256(payload).hexdigest(),
            args.processing_mode,
            job_id or "",
            env=env,
        )
        print(result.stdout.strip(), flush=True)
        if args.dispatch_fake_worker:
            command = [
                "python",
                "-m",
                "app.dispatcher",
                "--once",
                "--adapter",
                "local-fake",
            ]
            interrupted_report = None
            if args.recover_publication:
                interrupt_publication = """
import json
from dataclasses import asdict
from uuid import uuid4
from sqlalchemy import event, select
from app.database import engine
from app.db_models import Job, JobAttempt, JobOutput
from app.dispatcher import dispatch_once
from app.execution import LocalFakeWorkerAdapter

def fail_publication(connection):
    if connection.scalar(select(JobAttempt.result_manifest_key)) is not None:
        raise RuntimeError('Intentional smoke-test publication failure')

event.listen(engine, 'commit', fail_publication)
try:
    result = dispatch_once(engine, LocalFakeWorkerAdapter(engine), dispatcher_id=uuid4())
finally:
    event.remove(engine, 'commit', fail_publication)
assert result.status == 'publication_unresolved'
with engine.connect() as connection:
    attempt = connection.execute(select(JobAttempt.__table__)).one()
    assert attempt.phase == 'running' and attempt.execution_stopped_at is not None
    assert connection.scalar(select(Job.status)) == 'processing'
    assert connection.execute(select(JobOutput.__table__)).all() == []
    report = dict(asdict(result), local_worker_id=str(attempt.local_worker_id),
                  execution_stopped_at=attempt.execution_stopped_at.isoformat())
engine.dispose()
print(json.dumps(report, default=str))
"""
                interrupted = docker(
                    "run",
                    "--rm",
                    *common,
                    args.image,
                    "python",
                    "-c",
                    interrupt_publication,
                    env=env,
                )
                interrupted_report = json.loads(interrupted.stdout)
                assert interrupted_report["status"] == "publication_unresolved"
                dispatched = docker(
                    "run",
                    "--rm",
                    *common,
                    args.image,
                    *command,
                    "--reconcile-only",
                    env=env,
                )
            else:
                dispatched = docker(
                    "run", "--rm", *common, args.image, *command, env=env
                )
            report = json.loads(dispatched.stdout)
            assert report["status"] == (
                "recovered" if args.recover_publication else "completed"
            )
            if interrupted_report:
                for key in (
                    "job_id",
                    "attempt_id",
                    "invocation_id",
                    "worker_pid",
                    "manifest_key",
                ):
                    assert report[key] == interrupted_report[key]
                print(
                    "Fresh dispatcher recovered interrupted publication without a new execution",
                    flush=True,
                )
            assert report["job_id"] == job_id and report["worker_pid"] > 1
            assert report["manifest_key"].endswith("/manifest.json")
            repeated = docker("run", "--rm", *common, args.image, *command, env=env)
            assert json.loads(repeated.stdout)["status"] == "idle"
            dispatch_check = """
import sqlite3
import sys
from uuid import UUID
from datetime import datetime
from pathlib import Path
from app.results import LocalResultStore, ResultIdentity, FAKE_RESULT_PROFILE
with sqlite3.connect('/var/lib/isojam/metadata/isojam.db') as connection:
    assert connection.execute('select status from jobs').fetchall() == [('completed',)]
    attempts = connection.execute('select phase, invocation_id, started_at, last_heartbeat_at, execution_stopped_at, execution_exit_code, finished_at from job_attempts').fetchall()
    assert len(attempts) == 1
    phase, invocation, started, heartbeat, stopped, exit_code, finished = attempts[0]
    assert phase == 'succeeded' and invocation is not None
    assert started is not None and heartbeat >= started and stopped >= heartbeat
    assert finished >= stopped and exit_code == 0
    worker_id, worker_pid = connection.execute('select local_worker_id, local_worker_pid from job_attempts').fetchone()
    assert UUID(worker_id) and worker_pid > 1
    if sys.argv[1]:
        assert UUID(worker_id) == UUID(sys.argv[1])
        assert datetime.fromisoformat(stopped) == datetime.fromisoformat(sys.argv[2]).replace(tzinfo=None)
    assert connection.execute('select count(*) from job_outputs').fetchone()[0] == 7
    selected_key, selected_hash = connection.execute('select result_manifest_key, result_manifest_sha256 from job_attempts').fetchone()
    output_rows = dict(connection.execute('select stem, path from job_outputs').fetchall())
    job_id, attempt_id = connection.execute('select job_id, id from job_attempts').fetchone()
identity = ResultIdentity(job_id=job_id, attempt_id=attempt_id, invocation_id=invocation)
bundle = LocalResultStore(Path('/var/lib/isojam/audio/results')).verify_bundle(identity)
assert bundle.manifest.profile_id == FAKE_RESULT_PROFILE.id and len(bundle.outputs) == 7
assert selected_key == bundle.manifest_key and selected_hash == bundle.manifest_sha256
assert output_rows == {artifact.stem: str(artifact.path) for artifact in bundle.outputs}
print('Separate worker published seven verified fake WAVs; job completed; second cycle idle')
print(__import__('json').dumps({artifact.stem: artifact.sha256 for artifact in bundle.outputs}))
"""
            checked = docker(
                "run",
                "--rm",
                *common,
                args.image,
                "python",
                "-c",
                dispatch_check,
                interrupted_report["local_worker_id"] if interrupted_report else "",
                interrupted_report["execution_stopped_at"]
                if interrupted_report
                else "",
                env=env,
            )
            lines = checked.stdout.strip().splitlines()
            print(lines[0], flush=True)
            expected_hashes = json.loads(lines[-1])
            # Restart the API after publication, then download via real HTTP.
            docker(
                "run",
                "--detach",
                "--name",
                name,
                *common,
                "--publish",
                "127.0.0.1::8000",
                args.image,
                env=env,
            )
            ports = json.loads(
                docker(
                    "inspect", "--format", "{{json .NetworkSettings.Ports}}", name
                ).stdout
            )
            port = int(ports["8000/tcp"][0]["HostPort"])
            wait_for_health(name, port)
            status, login = post_json(port, "/login", credentials)
            assert status == 200
            headers = {"Authorization": "Bearer " + login["access_token"]}
            status, completed = request(port, "GET", f"/jobs/{job_id}", headers=headers)
            assert status == 200 and completed["status"] == "completed"
            assert set(completed["outputs"]) == set(expected_hashes)
            status, replayed = post_json(
                port,
                "/jobs",
                {"upload_id": upload_id},
                {**headers, "Idempotency-Key": "container-request"},
            )
            assert status == 200 and replayed == completed
            for stem, expected_hash in expected_hashes.items():
                req = Request(
                    f"http://127.0.0.1:{port}" + completed["outputs"][stem],
                    headers=headers,
                )
                with urlopen(req, timeout=3) as response:
                    raw = response.read(64 * 1024 + 1)
                    assert (
                        response.status == 200
                        and hashlib.sha256(raw).hexdigest() == expected_hash
                    )
            status, _ = request(port, "GET", completed["outputs"]["vocals"])
            assert status == 401
            print(
                "Restarted API served all seven owned WAVs with exact published hashes; keyed replay preserved completion",
                flush=True,
            )
        print("Container smoke passed", flush=True)
    except Exception:
        if owns_resource("container", name, marker):
            logs = docker("logs", "--tail", "40", name, check=False)
            print(logs.stdout + logs.stderr, flush=True)
        raise
    finally:
        # Never prune shared resources; remove only objects carrying this run's label.
        if owns_resource("container", name, marker):
            docker("rm", "--force", name, check=False)
        if owns_resource("volume", volume, marker):
            docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
