import os
import subprocess
import sys
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

import app.model_execution as transport
import app.model_worker as worker
from app.db_models import Job, JobAttempt, JobOutput
from app.dispatcher import dispatch_once, reconcile_once
from app.execution import WorkerExecutionFailed, WorkerInvocation, WorkerLaunchError
from app.model_execution import LocalModelWorkerAdapter
from app.repositories import job_reservations as reservations
from app.results import ResultValidationError
from tests.factories import create_test_upload, create_test_user, make_wav_bytes
from tests.model_worker_helpers import CpuModel


@pytest.fixture
def model_job(reservation_engine, clock):
    # Subprocesses have a real clock, unlike in-process authority tests.
    clock.now = datetime.now(UTC)
    root = transport.get_audio_storage_dir()
    (root / "uploads").mkdir(parents=True)
    with Session(reservation_engine) as session:
        user = create_test_user(session)
        upload = create_test_upload(session, user)
        job = Job(upload_id=upload.id, status="pending", execution_backend="queued")
        session.add(job)
        session.flush()
        job_id = job.id
        (root / "uploads" / upload.stored_filename).write_bytes(
            make_wav_bytes(frames=88200)
        )
        session.commit()
    return job_id


@pytest.fixture
def model_invocation(reservation_engine, model_job):
    token = reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4())
    assert reservations.begin_submission(reservation_engine, token)
    invocation = WorkerInvocation.from_reservation(token, uuid4()).model_copy(
        update={"local_worker_id": uuid4()}
    )
    return invocation


@pytest.fixture
def cpu_child(monkeypatch):
    original = subprocess.Popen

    def install(mode="success"):
        def launch(command, **kwargs):
            assert command == [sys.executable, "-m", "app.model_worker"]
            assert kwargs["start_new_session"]
            assert "ISOJAM_JWT_SECRET_KEY" not in kwargs["env"]
            return original(
                [
                    sys.executable,
                    "-c",
                    f"from tests.model_worker_helpers import child_main; raise SystemExit(child_main({mode!r}))",
                ],
                **kwargs,
            )

        monkeypatch.setattr(transport.subprocess, "Popen", launch)

    return install


def test_permission_denial_never_loads_model(reservation_engine, model_invocation):
    factory = Mock(side_effect=CpuModel)
    first = worker.run_model_worker(
        reservation_engine, model_invocation, model_session_factory=factory
    )
    assert first.status == "result_ready"
    assert (
        worker.run_model_worker(
            reservation_engine, model_invocation, model_session_factory=factory
        ).status
        == "permission_denied"
    )
    assert factory.call_count == 1
    with Session(reservation_engine) as session:
        assert (
            session.get(JobAttempt, model_invocation.attempt_id).execution_stopped_at
            is None
        )
        assert session.get(Job, model_invocation.job_id).status == "processing"
        assert not session.scalars(select(JobOutput)).all()


def test_lost_authorization_ack_never_loads_model(
    reservation_engine, model_invocation, monkeypatch
):
    def lost_ack(*args, **kwargs):
        raise RuntimeError("lost permission acknowledgement")

    monkeypatch.setattr(reservations, "authorize_execution", lost_ack)
    factory = Mock()
    with pytest.raises(RuntimeError, match="lost permission"):
        worker.run_model_worker(
            reservation_engine, model_invocation, model_session_factory=factory
        )
    factory.assert_not_called()


def test_initial_heartbeat_denial_never_loads_model(
    reservation_engine, model_invocation, monkeypatch
):
    monkeypatch.setattr(reservations, "record_heartbeat", lambda *args, **kwargs: False)
    factory = Mock()
    assert (
        worker.run_model_worker(
            reservation_engine, model_invocation, model_session_factory=factory
        ).status
        == "heartbeat_rejected"
    )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "mode,expected",
    [("success", "completed"), ("failure", "failed"), ("close-failure", "recovered")],
)
def test_dispatch_cycle_uses_real_profile_and_does_not_repeat_inference(
    reservation_engine,
    model_job,
    cpu_child,
    mode,
    expected,
):
    cpu_child(mode)
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    result = dispatch_once(
        reservation_engine, adapter, dispatcher_id=uuid4(), profile=adapter.profile
    )
    assert result.status == expected and result.job_id == model_job
    assert (
        dispatch_once(
            reservation_engine, adapter, dispatcher_id=uuid4(), profile=adapter.profile
        ).status
        == "idle"
    )
    assert len(list((adapter.audio_storage_dir / "worker-logs").glob("*.log"))) == 1


def test_surviving_process_group_prevents_stop_proof(
    reservation_engine, model_invocation, cpu_child, monkeypatch
):
    cpu_child()
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    monkeypatch.setattr(adapter, "_group_gone", lambda pid: False)
    with pytest.raises(WorkerLaunchError, match="process group has not ended"):
        adapter.submit(model_invocation)
    assert (
        reconcile_once(reservation_engine, adapter.results, profile=adapter.profile)
        is None
    )
    with Session(reservation_engine) as session:
        assert (
            session.get(JobAttempt, model_invocation.attempt_id).execution_stopped_at
            is None
        )


def test_duplicate_child_cannot_replace_original_stop_proof(
    reservation_engine, model_invocation, cpu_child
):
    cpu_child()
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    first = adapter.submit(model_invocation)
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, model_invocation.attempt_id)
        stopped, launch_id = attempt.execution_stopped_at, attempt.local_worker_id
    assert adapter.submit(model_invocation).status == "permission_denied"
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, model_invocation.attempt_id)
        assert (
            attempt.local_worker_pid,
            attempt.execution_stopped_at,
            attempt.local_worker_id,
        ) == (first.worker_pid, stopped, launch_id)


def test_unreadable_storage_cannot_be_treated_as_no_manifest(
    reservation_engine, model_invocation, cpu_child, monkeypatch
):
    cpu_child("failure")
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    with pytest.raises(WorkerExecutionFailed):
        adapter.submit(model_invocation)

    def unreadable(identity):
        raise ResultValidationError("unreadable result storage")

    monkeypatch.setattr(adapter.results, "has_manifest", unreadable)
    assert (
        reconcile_once(
            reservation_engine, adapter.results, profile=adapter.profile
        ).status
        == "result_invalid"
    )
    with Session(reservation_engine) as session:
        assert session.get(Job, model_invocation.job_id).status == "processing"


@pytest.mark.parametrize("payload", [b"{}", b"invalid-json", b"x" * 4097])
def test_model_worker_rejects_invalid_input_before_database(
    tmp_path, monkeypatch, payload
):
    path = tmp_path / "not-created" / "db.sqlite"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = subprocess.run(
        [sys.executable, "-m", "app.model_worker"],
        input=payload,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2 and result.stdout == b""
    assert not path.parent.exists()


def test_model_worker_heartbeats_and_closes_session_on_failure(
    reservation_engine, model_invocation, monkeypatch
):
    import time

    original = reservations.record_heartbeat
    contacts = []

    def contact(*args, **kwargs):
        contacts.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(reservations, "record_heartbeat", contact)

    class FailingModel:
        closed = False

        def infer(self, *args, **kwargs):
            deadline = time.monotonic() + 3
            while len(contacts) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            raise RuntimeError("model failed")

        def close(self):
            self.closed = True

    model = FailingModel()
    with pytest.raises(RuntimeError, match="model failed"):
        worker.run_model_worker(
            reservation_engine,
            model_invocation,
            model_session_factory=lambda: model,
            heartbeat_interval_seconds=0.01,
        )
    assert len(contacts) >= 2 and model.closed


def test_real_transport_separates_native_and_python_logs_and_confirms_stop(
    reservation_engine,
    model_invocation,
    cpu_child,
    monkeypatch,
):
    cpu_child()
    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", "never-forward-this-secret")
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    report = adapter.submit(model_invocation)
    assert report.status == "result_ready" and report.worker_pid != os.getpid()
    assert (
        len(
            adapter.results.verify_bundle(
                model_invocation.result_identity(), profile=adapter.profile
            ).outputs
        )
        == 7
    )
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, model_invocation.attempt_id)
        assert (
            attempt.execution_stopped_at is not None
            and attempt.execution_exit_code == 0
        )
        assert attempt.local_worker_pid == report.worker_pid
        assert attempt.local_worker_id != model_invocation.local_worker_id
        assert attempt.phase == "running"
    logs = list((adapter.audio_storage_dir / "worker-logs").glob("*.log"))
    assert len(logs) == 1 and logs[0].stat().st_mode & 0o777 == 0o600
    assert "model native stdout diagnostic" in logs[0].read_text()
    assert "model Python diagnostic" in logs[0].read_text()
    assert "never-forward-this-secret" not in logs[0].read_text()


@pytest.mark.parametrize(
    "mode,expected",
    [("failure", "failed"), ("close-failure", "recovered"), ("success", "completed")],
)
def test_dispatcher_real_profile_publishes_or_finalizes_only_confirmed_failure(
    reservation_engine,
    model_invocation,
    cpu_child,
    mode,
    expected,
):
    cpu_child(mode)
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    # The fixture already reserved/submitted this invocation. Deliver it through
    # a real child, then exercise the same restart path used after a lost reply.
    from app.execution import WorkerExecutionFailed

    try:
        adapter.submit(model_invocation)
    except WorkerExecutionFailed:
        pass
    result = reconcile_once(
        reservation_engine, adapter.results, profile=adapter.profile
    )
    assert result.status == ("recovered" if expected == "completed" else expected)
    with Session(reservation_engine) as session:
        job = session.get(Job, model_invocation.job_id)
        assert job.status == ("failed" if expected == "failed" else "completed")
        assert len(session.scalars(select(JobOutput)).all()) == (
            0 if expected == "failed" else 7
        )
    assert (
        dispatch_once(
            reservation_engine, adapter, dispatcher_id=uuid4(), profile=adapter.profile
        ).status
        == "idle"
    )


def test_timeout_records_killed_worker_before_failure(
    reservation_engine, model_invocation, cpu_child
):
    cpu_child("stall")
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=3)
    from app.execution import WorkerExecutionFailed

    with pytest.raises(WorkerExecutionFailed):
        adapter.submit(model_invocation)
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, model_invocation.attempt_id)
        assert (
            attempt.execution_exit_code == -9
            and attempt.execution_stopped_at is not None
        )
    assert (
        reconcile_once(
            reservation_engine, adapter.results, profile=adapter.profile
        ).status
        == "failed"
    )


def test_stop_commit_failure_holds_capacity(
    reservation_engine, model_invocation, cpu_child
):
    cpu_child("failure")

    def fail_commit(connection):
        if connection.scalar(select(JobAttempt.execution_stopped_at)) is not None:
            raise RuntimeError("stop commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    try:
        with pytest.raises(WorkerLaunchError, match="stop evidence"):
            adapter.submit(model_invocation)
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    assert (
        reconcile_once(reservation_engine, adapter.results, profile=adapter.profile)
        is None
    )
    with Session(reservation_engine) as session:
        assert session.get(Job, model_invocation.job_id).status == "processing"


def test_invalid_existing_manifest_is_not_retired(
    reservation_engine, model_invocation, cpu_child
):
    cpu_child("close-failure")
    adapter = LocalModelWorkerAdapter(reservation_engine, timeout_seconds=20)
    from app.execution import WorkerExecutionFailed

    with pytest.raises(WorkerExecutionFailed):
        adapter.submit(model_invocation)
    manifest = adapter.results.root / adapter.results.manifest_key(
        model_invocation.result_identity()
    )
    manifest.chmod(0o600)
    manifest.write_text("damaged manifest")
    assert (
        reconcile_once(
            reservation_engine, adapter.results, profile=adapter.profile
        ).status
        == "result_invalid"
    )
    with Session(reservation_engine) as session:
        assert session.get(Job, model_invocation.job_id).status == "processing"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--once", "--adapter", "local-model", "--worker-timeout-seconds", "7201"],
    ],
)
def test_model_cli_help_or_invalid_settings_never_create_database(
    tmp_path, monkeypatch, arguments
):
    path = tmp_path / "not-created" / "db.sqlite"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = subprocess.run(
        [sys.executable, "-m", "app.dispatcher", *arguments],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == (0 if arguments == ["--help"] else 2)
    assert not path.parent.exists()
