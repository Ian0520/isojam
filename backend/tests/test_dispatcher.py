import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.execution as execution
import app.storage as storage
from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput
from app.dispatcher import dispatch_once
from app.execution import LocalFakeWorkerAdapter, WorkerLaunchError
from app.fake_worker import run_fake_worker
from app.main import create_app
from app.results import ResultIdentity
from app.security import create_access_token
from tests.factories import create_test_upload, create_test_user


@pytest.fixture
def queued_job(reservation_engine):
    with Session(reservation_engine) as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = Job(upload_id=upload.id, status="pending", execution_backend="queued")
        session.add(job)
        session.commit()
        return job.id


def attempt_state(engine, job_id):
    with Session(engine) as session:
        attempt = session.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id))
        job = session.get(Job, job_id)
        return attempt, job


def test_cycle_runs_separate_fake_worker_and_retains_slot_after_contact(
    reservation_engine,
    queued_job,
):
    adapter = LocalFakeWorkerAdapter(reservation_engine)
    result = dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    assert result.status == "worker_result_ready"
    assert result.job_id == queued_job
    assert result.worker_pid != os.getpid()
    identity = ResultIdentity(
        job_id=result.job_id,
        attempt_id=result.attempt_id,
        invocation_id=result.invocation_id,
    )
    bundle = adapter.results.verify_bundle(identity)
    assert result.manifest_key == bundle.manifest_key
    before = {
        path.name: (path.stat().st_ino, path.read_bytes())
        for path in (adapter.results.root / bundle.manifest_key).parent.iterdir()
    }
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert attempt.id == result.attempt_id
    assert attempt.invocation_id == result.invocation_id
    assert attempt.phase == "running"
    assert attempt.last_heartbeat_at >= attempt.started_at
    assert attempt.finished_at is None
    assert job.status == "processing"
    assert (
        dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4()).status
        == "idle"
    )
    with Session(reservation_engine) as session:
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
        assert session.scalar(select(func.count()).select_from(JobOutput)) == 0
    assert {
        path.name: (path.stat().st_ino, path.read_bytes())
        for path in (adapter.results.root / bundle.manifest_key).parent.iterdir()
    } == before


def test_submission_is_committed_and_write_lock_released_before_adapter_call(
    reservation_engine,
    queued_job,
):
    def submit(invocation):
        observed = create_engine(reservation_engine.url)
        try:
            with observed.connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout = 25")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                row = connection.exec_driver_sql(
                    "SELECT phase, invocation_id, execution_authorization_expires_at FROM job_attempts"
                ).one()
                assert row.phase == "submitting"
                assert row.invocation_id is None
                assert row.execution_authorization_expires_at is not None
                connection.rollback()
        finally:
            observed.dispose()
        return run_fake_worker(reservation_engine, invocation)

    result = dispatch_once(
        reservation_engine, SimpleNamespace(submit=submit), dispatcher_id=uuid4()
    )
    assert result.status == "worker_result_ready"


@pytest.mark.parametrize("fail_at", [1, 2])
def test_failed_authority_commit_never_launches_worker(
    reservation_engine, queued_job, fail_at
):
    adapter = Mock()
    commits = 0

    def fail_commit(connection):
        nonlocal commits
        commits += 1
        if commits == fail_at:
            raise RuntimeError("authority commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="authority commit failed"):
            dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    adapter.submit.assert_not_called()
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert job.status == "pending"
    if fail_at == 1:
        assert attempt is None
    else:
        assert attempt.phase == "reserved"
        assert attempt.execution_authorization_expires_at is None
        assert attempt.invocation_id is None


def test_worker_permission_denial_does_not_record_contact_or_release_capacity(
    reservation_engine,
    queued_job,
):
    adapter = LocalFakeWorkerAdapter(reservation_engine)

    def expire_then_submit(invocation):
        with Session(reservation_engine) as session:
            session.get(
                JobAttempt, invocation.attempt_id
            ).execution_authorization_expires_at = datetime.now(UTC) - timedelta(
                seconds=1
            )
            session.commit()
        return adapter.submit(invocation)

    result = dispatch_once(
        reservation_engine,
        SimpleNamespace(submit=expire_then_submit),
        dispatcher_id=uuid4(),
    )
    assert result.status == "worker_permission_denied"
    assert result.manifest_key is None and not adapter.results.root.exists()
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert attempt.phase == "submitting"
    assert attempt.invocation_id is None
    assert attempt.started_at is None
    assert attempt.last_heartbeat_at is None
    assert job.status == "pending"
    assert (
        dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4()).status
        == "idle"
    )


def test_lost_report_after_worker_contact_preserves_running_attempt_and_never_resubmits(
    reservation_engine,
    queued_job,
):
    real_adapter = LocalFakeWorkerAdapter(reservation_engine)
    deliveries = []

    def lose_report(invocation):
        deliveries.append(invocation)
        assert real_adapter.submit(invocation).status == "result_ready"
        raise WorkerLaunchError("report lost after contact")

    adapter = SimpleNamespace(submit=lose_report)
    result = dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    assert result.status == "submission_unresolved"
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert attempt.invocation_id == result.invocation_id
    assert attempt.phase == "running" and attempt.last_heartbeat_at is not None
    assert job.status == "processing"
    assert (
        dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4()).status
        == "idle"
    )
    assert len(deliveries) == 1
    assert (
        len(real_adapter.results.verify_bundle(deliveries[0].result_identity()).outputs)
        == 7
    )


def test_spawn_failure_preserves_submission_intent_and_never_retries(
    reservation_engine,
    queued_job,
    monkeypatch,
):
    launch = Mock(side_effect=OSError("process could not start"))
    monkeypatch.setattr(execution.subprocess, "run", launch)
    adapter = LocalFakeWorkerAdapter(reservation_engine)
    result = dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    assert result.status == "submission_unresolved"
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert attempt.phase == "submitting" and attempt.invocation_id is None
    assert job.status == "pending"
    assert (
        dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4()).status
        == "idle"
    )
    launch.assert_called_once()


def test_timeout_kills_and_waits_for_direct_child_but_does_not_publish_or_free_slot(
    reservation_engine,
    queued_job,
    monkeypatch,
    tmp_path,
):
    run = subprocess.run
    marker = tmp_path / "worker.pid"
    stalled_worker = """
import os
import sys
import time
from pathlib import Path
from app.execution import WorkerInvocation
from app.fake_worker import run_fake_worker
from app.database import engine
invocation = WorkerInvocation.model_validate_json(sys.stdin.buffer.read())
assert run_fake_worker(engine, invocation).status == 'result_ready'
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""

    def stall(command, **kwargs):
        return run([sys.executable, "-c", stalled_worker, str(marker)], **kwargs)

    monkeypatch.setattr(execution.subprocess, "run", stall)
    result = dispatch_once(
        reservation_engine,
        LocalFakeWorkerAdapter(reservation_engine, timeout_seconds=3),
        dispatcher_id=uuid4(),
    )
    assert result.status == "submission_unresolved"
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    attempt, job = attempt_state(reservation_engine, queued_job)
    assert attempt.phase == "running" and attempt.last_heartbeat_at is not None
    assert attempt.finished_at is None and job.status == "processing"
    identity = ResultIdentity(
        job_id=result.job_id,
        attempt_id=result.attempt_id,
        invocation_id=result.invocation_id,
    )
    assert (
        len(
            LocalFakeWorkerAdapter(reservation_engine)
            .results.verify_bundle(identity)
            .outputs
        )
        == 7
    )
    assert (
        dispatch_once(reservation_engine, Mock(), dispatcher_id=uuid4()).status
        == "idle"
    )


def competing_dispatcher(database_url, barrier, results):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    waited = False

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        nonlocal waited
        if statement == "BEGIN IMMEDIATE" and not waited:
            waited = True
            barrier.wait(timeout=15)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        result = dispatch_once(
            engine,
            LocalFakeWorkerAdapter(engine),
            dispatcher_id=uuid4(),
            busy_timeout_ms=5000,
        )
        results.put(asdict(result))
    except Exception as error:
        results.put({"error": repr(error)})
    finally:
        engine.dispose()


def test_independent_dispatchers_launch_one_invocation(reservation_engine, queued_job):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=competing_dispatcher,
            args=(str(reservation_engine.url), barrier, results),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(item.get("status", "error") for item in outcomes) == [
            "idle",
            "worker_result_ready",
        ], outcomes
        winner = next(
            item for item in outcomes if item["status"] == "worker_result_ready"
        )
        assert winner["worker_pid"] not in {os.getpid(), *(p.pid for p in processes)}
        attempt, job = attempt_state(reservation_engine, queued_job)
        assert attempt.invocation_id == winner["invocation_id"]
        assert attempt.last_heartbeat_at is not None and job.status == "processing"
        with Session(reservation_engine) as session:
            assert session.scalar(select(func.count()).select_from(JobAttempt)) == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


def test_queued_api_acceptance_then_dispatcher_cli_works_after_api_shutdown(
    reservation_engine,
    jwt_secret_key,
    wav_bytes,
    monkeypatch,
    tmp_path,
):
    factory = sessionmaker(reservation_engine)
    with factory() as session:
        user = create_test_user(session)
        user_id = user.id
        session.commit()
    token = create_access_token(
        user_id=user_id, secret_key=jwt_secret_key, expires_delta=timedelta(minutes=5)
    )
    headers = {"Authorization": f"Bearer {token}"}
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path / "uploads")
    app = create_app(
        db_session_factory=factory,
        jwt_secret_key=jwt_secret_key,
        processing_mode="queued",
        model_session_factory=Mock(
            side_effect=AssertionError("API must not load inference")
        ),
    )
    with TestClient(app) as client:
        uploaded = client.post(
            "/uploads",
            files={"audio_file": ("song.wav", wav_bytes, "audio/wav")},
            headers=headers,
        )
        assert uploaded.status_code == 200
        accepted = client.post(
            "/jobs", json={"upload_id": uploaded.json()["id"]}, headers=headers
        )
        assert accepted.status_code == 200
        job_id = UUID(accepted.json()["id"])
    # Both dispatcher and worker run outside the now-stopped API process.
    with subprocess.Popen(
        [sys.executable, "-m", "app.dispatcher", "--once", "--adapter", "local-fake"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=35)
        assert process.returncode == 0, stderr
        report = json.loads(stdout)
        assert report["worker_pid"] not in {os.getpid(), process.pid}
    assert (
        report["status"] == "worker_result_ready" and UUID(report["job_id"]) == job_id
    )
    identity = ResultIdentity(
        job_id=job_id,
        attempt_id=report["attempt_id"],
        invocation_id=report["invocation_id"],
    )
    assert (
        LocalFakeWorkerAdapter(reservation_engine)
        .results.verify_bundle(identity)
        .manifest_key
        == report["manifest_key"]
    )
    with TestClient(app) as restarted:
        response = restarted.get(f"/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        assert (
            response.json()["status"] == "processing"
            and response.json()["outputs"] == {}
        )
        assert (
            restarted.get(f"/jobs/{job_id}/outputs/vocals", headers=headers).status_code
            == 409
        )
    again = subprocess.run(
        [sys.executable, "-m", "app.dispatcher", "--once", "--adapter", "local-fake"],
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["status"] == "idle"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"authorization_ttl_seconds": 0},
        {"reservation_ttl_seconds": True},
        {"busy_timeout_ms": -1},
    ],
)
def test_invalid_dispatch_settings_create_no_attempt(
    reservation_engine, queued_job, kwargs
):
    adapter = Mock()
    with pytest.raises(ValueError):
        dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4(), **kwargs)
    adapter.submit.assert_not_called()
    assert attempt_state(reservation_engine, queued_job)[0] is None
