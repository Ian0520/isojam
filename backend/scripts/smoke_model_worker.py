"""Verify real queued inference, API restart and worker interruption on Linux/CUDA.

Uses only synthetic recordings, a fresh migrated database and disposable storage.
Requires the inference environment and cached model assets; never downloads them
as a preflight fallback. This is intentionally separate from CPU-only CI.
"""

import hashlib
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import numpy as np
import soundfile as sf
from sqlalchemy import create_engine, select

from app.db_models import Job, JobAttempt, JobOutput, Upload
from app.model_processing import get_model_result_profile
from app.results import LocalResultStore, ResultIdentity

BACKEND = Path(__file__).resolve().parents[1]
MAX_HTTP_BYTES = 16 * 1024 * 1024


def wav_bytes(seconds):
    rate = 44100
    t = np.arange(rate * seconds, dtype=np.float32) / rate
    samples = np.column_stack(
        (0.12 * np.sin(2 * np.pi * 220 * t), 0.1 * np.sin(2 * np.pi * 330 * t))
    )
    output = io.BytesIO()
    sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
    return output.getvalue()


def request(base, method, path, *, body=None, token=None, headers=None):
    headers = dict(headers or {})
    if body is not None and not isinstance(body, bytes):
        body = json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(base + path, data=body, headers=headers, method=method)
    try:
        response = urlopen(req, timeout=5)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raise RuntimeError("Smoke response exceeded its byte limit")
        value = (
            json.loads(raw)
            if "json" in response.headers.get("Content-Type", "")
            else raw
        )
        return response.status, value


def wait_until(description, predicate, *, processes=(), timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for process in processes):
            raise RuntimeError(f"A service exited while waiting for {description}")
        try:
            if value := predicate():
                return value
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"Timed out waiting for {description}")


def start_process(arguments, env, root, name, **kwargs):
    with (
        (root / f"{name}.stdout").open("ab") as stdout,
        (root / f"{name}.stderr").open("ab") as stderr,
    ):
        return subprocess.Popen(
            [sys.executable, *arguments],
            cwd=BACKEND,
            env=env,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            **kwargs,
        )


def stop_process(process, *, timeout=10):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def snapshot(engine, job_id):
    with engine.connect() as connection:
        return dict(
            connection.execute(
                select(
                    Job.status,
                    JobAttempt.id.label("attempt_id"),
                    JobAttempt.invocation_id,
                    JobAttempt.local_worker_pid,
                    JobAttempt.execution_exit_code,
                    JobAttempt.execution_stopped_at,
                )
                .select_from(Job)
                .outerjoin(JobAttempt, JobAttempt.job_id == Job.id)
                .where(Job.id == UUID(job_id))
                .order_by(JobAttempt.attempt_number.desc())
                .limit(1)
            )
            .mappings()
            .one()
        )


def processing_started(engine, job_id, audio):
    state = snapshot(engine, job_id)
    if state["status"] != "processing" or state["execution_stopped_at"] is not None:
        return False
    for log in (audio / "worker-logs").glob(f"{state['invocation_id']}-*.log"):
        with log.open() as source:
            if "Processing track" in source.read(512 * 1024):
                return state
    return False


def kill_owned_worker(state, database):
    pid = state["local_worker_pid"]
    descriptor = os.pidfd_open(pid)
    try:
        environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if f"ISOJAM_DATABASE_PATH={database}".encode() not in environment:
            raise RuntimeError(
                "Refusing to signal a worker outside this smoke's database"
            )
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)


def register(base, email):
    credentials = {"email": email, "password": "synthetic-smoke-password-only"}
    assert request(base, "POST", "/register", body=credentials)[0] == 201
    status, login = request(base, "POST", "/login", body=credentials)
    assert status == 200, "Smoke login failed"
    return login["access_token"]


def submit(base, token, recording, key):
    boundary = uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="synthetic.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        + recording
        + f"\r\n--{boundary}--\r\n".encode()
    )
    status, upload = request(
        base,
        "POST",
        "/uploads",
        token=token,
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert status == 200, "Synthetic upload failed"
    status, job = request(
        base,
        "POST",
        "/jobs",
        token=token,
        body={"upload_id": upload["id"]},
        headers={"Idempotency-Key": key},
    )
    assert status == 200 and job["status"] == "pending"
    return job, upload


def verify_downloads(base, token, other_token, job_id, engine, audio):
    status, job = request(base, "GET", f"/jobs/{job_id}", token=token)
    assert status == 200 and job["status"] == "completed"
    state = snapshot(engine, job_id)
    identity = ResultIdentity(
        job_id=job_id,
        attempt_id=state["attempt_id"],
        invocation_id=state["invocation_id"],
    )
    bundle = LocalResultStore(audio / "results").verify_bundle(
        identity, profile=get_model_result_profile(max_duration_seconds=600)
    )
    assert len(job["outputs"]) == 7 and state["execution_exit_code"] == 0
    assert not Path(f"/proc/{state['local_worker_pid']}").exists()
    for artifact in bundle.outputs:
        url = job["outputs"][artifact.stem]
        status, raw = request(base, "GET", url, token=token)
        assert status == 200 and hashlib.sha256(raw).hexdigest() == artifact.sha256
    first_url = next(iter(job["outputs"].values()))
    assert request(base, "GET", first_url)[0] == 401
    assert request(base, "GET", first_url, token=other_token)[0] == 404
    assert request(base, "GET", f"/jobs/{job_id}", token=other_token)[0] == 404
    return job


def run_smoke(root):
    database, audio = root / "metadata.db", root / "audio"
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700)
    env = os.environ.copy()
    env.update(
        ISOJAM_DATABASE_PATH=str(database),
        ISOJAM_AUDIO_STORAGE_DIR=str(audio),
        ISOJAM_PROCESSING_MODE="queued",
        ISOJAM_JWT_SECRET_KEY=secrets.token_hex(32),
        ISOJAM_MAX_AUDIO_DURATION_SECONDS="600",
        ISOJAM_MAX_UPLOAD_BYTES=str(200 * 1024 * 1024),
        ISOJAM_MAX_UNFINISHED_JOBS_PER_USER="2",
        ALEMBIC_DATABASE_URL="sqlite:///" + str(database),
        PYTHONUNBUFFERED="1",
        TMPDIR=str(scratch),
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    engine = create_engine("sqlite:///" + str(database))
    api = dispatcher = None
    jobs = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"

    def start_api():
        process = start_process(
            [
                "-m",
                "uvicorn",
                "app.main:app",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
            ],
            env,
            root,
            "api",
            pass_fds=(listener.fileno(),),
        )
        try:
            wait_until(
                "API health",
                lambda: request(base, "GET", "/health")[0] == 200,
                processes=(process,),
                timeout=30,
            )
        except BaseException:
            stop_process(process)
            raise
        return process

    try:
        api = start_api()
        token = register(base, "owner@example.com")
        other_token = register(base, "other@example.com")
        dispatcher = start_process(
            [
                "-m",
                "app.dispatcher",
                "--serve",
                "--adapter",
                "local-model",
                "--poll-interval-seconds",
                "0.1",
                "--worker-timeout-seconds",
                "120",
            ],
            env,
            root,
            "dispatcher",
        )
        wait_until(
            "initial idle cycle",
            lambda: '"idle"' in (root / "dispatcher.stdout").read_text(),
            processes=(dispatcher,),
        )
        recording = wav_bytes(20)
        first, upload = submit(base, token, recording, "restart-check")
        jobs.append(first["id"])
        running = wait_until(
            "real inference before API restart",
            lambda: processing_started(engine, first["id"], audio),
            processes=(api, dispatcher),
        )
        stop_process(api)
        api = None
        assert dispatcher.poll() is None
        assert Path(f"/proc/{running['local_worker_pid']}").exists()
        api = start_api()
        wait_until(
            "completion after API restart",
            lambda: snapshot(engine, first["id"])["status"] == "completed",
            processes=(api, dispatcher),
        )
        after = snapshot(engine, first["id"])
        assert (
            after["attempt_id"],
            after["invocation_id"],
            after["local_worker_pid"],
        ) == (
            running["attempt_id"],
            running["invocation_id"],
            running["local_worker_pid"],
        )
        completed = verify_downloads(
            base, token, other_token, first["id"], engine, audio
        )
        status, replayed = request(
            base,
            "POST",
            "/jobs",
            token=token,
            body={"upload_id": upload["id"]},
            headers={"Idempotency-Key": "restart-check"},
        )
        assert status == 200 and replayed == completed
        with engine.connect() as connection:
            filename = connection.scalar(
                select(Upload.stored_filename).where(Upload.id == UUID(upload["id"]))
            )
        assert (audio / "uploads" / filename).read_bytes() == recording
        print(
            "API restart preserved one real invocation, seven download hashes, ownership and submission replay",
            flush=True,
        )

        interrupted, _ = submit(base, token, recording, "interruption-check")
        jobs.append(interrupted["id"])
        running = wait_until(
            "real inference before interruption",
            lambda: processing_started(engine, interrupted["id"], audio),
            processes=(api, dispatcher),
        )
        time.sleep(0.5)
        assert snapshot(engine, interrupted["id"])["status"] == "processing"
        assert not LocalResultStore(audio / "results").has_manifest(
            ResultIdentity(
                job_id=interrupted["id"],
                attempt_id=running["attempt_id"],
                invocation_id=running["invocation_id"],
            )
        )
        kill_owned_worker(running, database)
        wait_until(
            "confirmed interrupted failure",
            lambda: snapshot(engine, interrupted["id"])["status"] == "failed",
            processes=(api, dispatcher),
        )
        stopped = snapshot(engine, interrupted["id"])
        assert (
            stopped["execution_exit_code"] == -signal.SIGKILL
            and stopped["execution_stopped_at"] is not None
        )
        assert dispatcher.poll() is None
        print(
            "Interrupted real worker acquired stop proof and failed without automatic inference retry",
            flush=True,
        )

        last, _ = submit(base, token, wav_bytes(4), "after-failure-check")
        jobs.append(last["id"])
        wait_until(
            "new work after confirmed failure",
            lambda: snapshot(engine, last["id"])["status"] == "completed",
            processes=(api, dispatcher),
        )
        verify_downloads(base, token, other_token, last["id"], engine, audio)
        dispatcher.terminate()
        assert dispatcher.wait(timeout=10) == 0
        with engine.connect() as connection:
            attempts = connection.execute(select(JobAttempt.job_id)).scalars().all()
            assert len(attempts) == 3 and set(attempts) == {UUID(job) for job in jobs}
            assert len(connection.execute(select(JobOutput)).all()) == 14
        print(
            "Same continuous dispatcher completed later work, then stopped cleanly; three jobs, three attempts",
            flush=True,
        )
    finally:
        stop_process(dispatcher, timeout=135)
        stop_process(api)
        listener.close()
        engine.dispose()


def main():
    if sys.platform != "linux":
        raise RuntimeError("Real queued smoke requires Linux/WSL2")
    import torch
    from bs_roformer.download import ensure_model_assets
    from bs_roformer.model_registry import DEFAULT_MODEL

    if not torch.cuda.is_available():
        raise RuntimeError("Real queued smoke requires an available CUDA GPU")
    ensure_model_assets(DEFAULT_MODEL, download_missing=False)
    with TemporaryDirectory(prefix="isojam-real-service-") as temporary:
        root = Path(temporary)
        try:
            run_smoke(root)
        except BaseException:
            for path in [
                *root.glob("*.stderr"),
                *root.glob("*.stdout"),
                *root.glob("audio/worker-logs/*.log"),
            ]:
                print(f"--- {path.name} ---", file=sys.stderr)
                print(path.read_text(errors="replace")[-12000:], file=sys.stderr)
            raise
    print("Real queued model smoke passed; disposable data removed", flush=True)


if __name__ == "__main__":
    main()
