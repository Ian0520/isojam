"""Verify the local Compose stack with disposable data; GPU inference is opt-in."""

import argparse
import hashlib
import io
import json
import math
import os
import secrets
import signal
import struct
import subprocess
import tempfile
import time
import uuid
import wave
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from smoke_container import docker, post_json, request

ROOT = Path(__file__).resolve().parents[2]
ACTIVE_MODEL = """
from pathlib import Path
print(any('Processing track' in p.read_text(errors='replace')[:524288]
          for p in Path('/var/lib/isojam/audio/worker-logs').glob('*.log')))
"""
SNAPSHOT = """
import hashlib, json, sqlite3
from pathlib import Path
c = sqlite3.connect('file:/var/lib/isojam/metadata/isojam.db?mode=ro', uri=True)
c.row_factory = sqlite3.Row
jobs = [dict(r) for r in c.execute('''SELECT j.id, j.status, a.id AS attempt_id,
    a.execution_exit_code, a.execution_stopped_at FROM jobs j
    LEFT JOIN job_attempts a ON a.job_id=j.id ORDER BY j.created_at,j.id''')]
outputs = {r['job_id'] + ':' + r['stem']: hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()
           for r in c.execute('SELECT job_id,stem,path FROM job_outputs')}
uploads = [hashlib.sha256((Path('/var/lib/isojam/audio/uploads') / r[0]).read_bytes()).hexdigest()
           for r in c.execute('SELECT stored_filename FROM uploads')]
c.close()
print(json.dumps(dict(jobs=jobs, outputs=outputs, uploads=uploads)))
"""


def recording(seconds):
    output = io.BytesIO()
    # Repeat a short non-silent stereo tone; no external audio or user files.
    cycle = b"".join(
        struct.pack(
            "<hh",
            int(3000 * math.sin(i * 2 * math.pi / 100)),
            int(2500 * math.sin(i * 2 * math.pi / 50)),
        )
        for i in range(44100)
    )
    with wave.open(output, "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(cycle * seconds)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", default="isojam-api:local-stack")
    parser.add_argument("--processor-image", default="isojam-processor:local-stack")
    parser.add_argument(
        "--models-dir",
        type=Path,
        help="Use real GPU inference with this read-only cache",
    )
    args = parser.parse_args()
    project = "isojam-stack-check-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix=project) as temporary:
        root = Path(temporary)
        settings = root / ".env"
        settings.write_text(
            f"ISOJAM_JWT_SECRET_KEY={secrets.token_hex(32)}\n"
            "ISOJAM_PORT=0\nISOJAM_REGISTRATION_MODE=open\n"
            f"ISOJAM_API_IMAGE={args.api_image}\nISOJAM_PROCESSOR_IMAGE={args.processor_image}\n"
        )
        settings.chmod(0o600)
        override = root / "override.yaml"
        if args.models_dir:
            directory = args.models_dir.resolve(strict=True)
            if not directory.is_dir():
                parser.error("--models-dir must be a directory")
            override.write_text(
                json.dumps(
                    {
                        "services": {
                            "processor": {
                                "volumes": [
                                    {
                                        "type": "bind",
                                        "source": str(directory),
                                        "target": "/var/cache/isojam/models",
                                        "read_only": True,
                                    }
                                ]
                            }
                        }
                    }
                )
            )
        else:
            command = [
                "python",
                "-c",
                "from app.processor import READY_FILE; import os,json,sys; "
                "READY_FILE.write_text(json.dumps({'pid':os.getpid()})); "
                "os.execv(sys.executable,[sys.executable,'-m','app.dispatcher','--serve',"
                "'--adapter','local-fake','--poll-interval-seconds','0.1'])",
            ]
            override.write_text(
                "services:\n  processor:\n    image: "
                + json.dumps(args.api_image)
                + "\n    gpus: !reset null\n    command: "
                + json.dumps(command)
                + "\n"
            )
        env = os.environ.copy()
        # Explicit disposable settings take precedence over inherited app settings.
        for name in list(env):
            if name.startswith("ISOJAM_") or name.startswith("COMPOSE_"):
                env.pop(name)
        base = [
            "docker",
            "compose",
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(settings),
            "-p",
            project,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(override),
        ]

        def compose(*parts, check=True, timeout=60):
            result = subprocess.run(
                [*base, *parts],
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if check and result.returncode:
                raise RuntimeError(result.stderr or result.stdout)
            return result.stdout.strip()

        service = None
        log = root / "compose.log"

        def start():
            with log.open("ab") as output:
                return subprocess.Popen(
                    [*base, "up", "--no-build", "--abort-on-container-failure"],
                    env=env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )

        def until(predicate, timeout=120):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if service.poll() is not None:
                    raise RuntimeError("Compose exited before the check finished")
                try:
                    value = predicate()
                    if value:
                        return value
                except (URLError, TimeoutError, ConnectionError):
                    pass
                time.sleep(0.5)
            raise RuntimeError("Timed out waiting for the local stack")

        def api_port():
            container = compose("ps", "-q", "api")
            if not container:
                return None
            ports = json.loads(
                docker(
                    "inspect", "--format", "{{json .NetworkSettings.Ports}}", container
                ).stdout
            )
            bindings = (ports or {}).get("8000/tcp")
            if not bindings:
                return None
            assert bindings[0]["HostIp"] == "127.0.0.1"
            port = int(bindings[0]["HostPort"])
            return port if request(port, "GET", "/health")[0] == 200 else None

        def snapshot():
            return json.loads(docker("exec", api_id, "python", "-c", SNAPSHOT).stdout)

        def job_status(job_id):
            status, job = request(
                port,
                "GET",
                f"/jobs/{job_id}",
                headers={"Authorization": "Bearer " + token},
            )
            assert status == 200, (status, job)
            return job["status"]

        def submit(port, token, seconds, key):
            data = recording(seconds)
            boundary = uuid.uuid4().hex
            body = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="synthetic.wav"\r\n'
                    "Content-Type: audio/wav\r\n\r\n"
                ).encode()
                + data
                + f"\r\n--{boundary}--\r\n".encode()
            )
            status, uploaded = request(
                port,
                "POST",
                "/uploads",
                body,
                {
                    "Authorization": "Bearer " + token,
                    "Content-Type": "multipart/form-data; boundary=" + boundary,
                },
            )
            assert status == 200
            status, job = post_json(
                port,
                "/jobs",
                {"upload_id": uploaded["id"]},
                {"Authorization": "Bearer " + token, "Idempotency-Key": key},
            )
            assert status == 200 and job["status"] == "pending"
            return job["id"], hashlib.sha256(data).hexdigest()

        try:
            service = start()
            port = until(api_port)
            api_id, processor_id = (
                compose("ps", "-q", "api"),
                compose("ps", "-q", "processor"),
            )
            credentials = {
                "email": "local-check@example.com",
                "password": "synthetic-check-password",
            }
            assert post_json(port, "/register", credentials)[0] == 201
            status, login = post_json(port, "/login", credentials)
            assert status == 200
            token = login["access_token"]
            job_id, upload_hash = submit(
                port, token, 20 if args.models_dir else 1, "first"
            )
            if args.models_dir:
                until(
                    lambda: (
                        job_status(job_id) == "processing"
                        and docker(
                            "exec", api_id, "python", "-c", ACTIVE_MODEL
                        ).stdout.strip()
                        == "True"
                    )
                )
                second_job, _ = submit(port, token, 4, "after-restart")
                print(
                    "Real inference is active; requesting Compose shutdown with another job pending",
                    flush=True,
                )
            else:
                until(lambda: job_status(job_id) == "completed")
            service.send_signal(signal.SIGINT)
            service.wait(timeout=180)
            states = {
                name: json.loads(
                    docker("inspect", "--format", "{{json .State}}", cid).stdout
                )
                for name, cid in [("api", api_id), ("processor", processor_id)]
            }
            # Uvicorn re-raises its handled SIGTERM after lifespan shutdown.
            assert not states["api"]["Running"] and states["api"]["ExitCode"] in {
                0,
                143,
            }, states
            assert (
                not states["processor"]["Running"]
                and states["processor"]["ExitCode"] == 0
            ), states
            assert not any(s["OOMKilled"] for s in states.values()), states
            assert states["api"]["FinishedAt"] <= states["processor"]["FinishedAt"]
            mounts = json.loads(
                docker("inspect", "--format", "{{json .Mounts}}", api_id).stdout
            )
            volume = next(
                m["Name"] for m in mounts if m["Destination"] == "/var/lib/isojam"
            )
            saved = json.loads(
                docker(
                    "run",
                    "--rm",
                    "--mount",
                    f"type=volume,src={volume},dst=/var/lib/isojam,readonly",
                    args.api_image,
                    "python",
                    "-c",
                    SNAPSHOT,
                ).stdout
            )
            completed = next(
                j for j in saved["jobs"] if j["id"] == uuid.UUID(job_id).hex
            )
            assert (
                completed["status"] == "completed"
                and completed["execution_exit_code"] == 0
            )
            assert completed["execution_stopped_at"] and upload_hash in saved["uploads"]
            if args.models_dir:
                assert sum(j["status"] == "pending" for j in saved["jobs"]) == 1
            print(
                "API stopped first; processor exited cleanly with committed stop proof and outputs",
                flush=True,
            )

            compose("rm", "--force", "api", "processor", "migrate")
            service = start()
            port = until(api_port)
            api_id = compose("ps", "-q", "api")
            until(lambda: job_status(job_id) == "completed")
            if args.models_dir:
                until(lambda: job_status(second_job) == "completed")
            status, login = post_json(port, "/login", credentials)
            assert status == 200
            token = login["access_token"]
            status, job = request(
                port,
                "GET",
                f"/jobs/{job_id}",
                headers={"Authorization": "Bearer " + token},
            )
            assert status == 200 and len(job["outputs"]) == 7
            for stem, path in job["outputs"].items():
                req = Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Authorization": "Bearer " + token},
                )
                with urlopen(req, timeout=10) as response:
                    assert (
                        hashlib.sha256(response.read()).hexdigest()
                        == saved["outputs"][uuid.UUID(job_id).hex + ":" + stem]
                    )
                assert request(port, "GET", path)[0] == 401
            after = snapshot()
            assert (
                next(j for j in after["jobs"] if j["id"] == completed["id"])[
                    "attempt_id"
                ]
                == completed["attempt_id"]
            )
            service.send_signal(signal.SIGINT)
            service.wait(timeout=180)
            print(
                "Replacement retained login, original attempt and seven exact downloads; queued work resumed",
                flush=True,
            )
        except BaseException:
            if log.exists():
                print(
                    log.read_text(errors="replace")[-16000:],
                    file=__import__("sys").stderr,
                )
            raise
        finally:
            # This project name is generated here, and the base file has no external volumes.
            compose("down", "--volumes", "--remove-orphans", timeout=180)
            if service is not None:
                service.wait(timeout=10)
    print("Compose smoke passed; disposable project and volumes removed", flush=True)


if __name__ == "__main__":
    main()
