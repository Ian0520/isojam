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
    args = parser.parse_args()
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
assert os.environ['ISOJAM_PROCESSING_MODE'] == 'disabled'
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
            status, _ = post_json(port, "/jobs", {"upload_id": upload_id}, headers)
            assert status == 503, f"Disabled processing returned {status}"
            status, _ = post_json(port, "/jobs", {"upload_id": upload_id})
            assert status == 401, f"Unauthenticated job submission returned {status}"
            print(
                f"{phase}: health, authentication, and job rejection passed", flush=True
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
    assert connection.execute('select count(*) from jobs').fetchone()[0] == 0
    rows = connection.execute('select id, stored_filename from uploads').fetchall()
    assert len(rows) == 1 and rows[0][0] == sys.argv[1].replace('-', '')
    stored = rows[0][1]
path = Path('/var/lib/isojam/audio/uploads') / stored
assert hashlib.sha256(path.read_bytes()).hexdigest() == sys.argv[2]
print('Account, upload metadata, and exact WAV bytes survived container replacement')
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
            env=env,
        )
        print(result.stdout.strip(), flush=True)
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
