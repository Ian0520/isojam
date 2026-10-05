import json
import os
import subprocess
import sys
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import Session

import app.execution as execution
from app.db_models import JobAttempt
from app.execution import LocalFakeWorkerAdapter, WorkerInvocation, WorkerLaunchError
from app.fake_worker import run_fake_worker
from app.repositories import job_reservations as reservations


@pytest.fixture
def invocation(reservation_engine, owned_reservation):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    return WorkerInvocation.from_reservation(owned_reservation, uuid4())


def test_repeated_worker_delivery_cannot_record_another_heartbeat(
    reservation_engine, invocation, clock
):
    assert run_fake_worker(reservation_engine, invocation).status == "contact_recorded"
    with Session(reservation_engine) as session:
        first_contact = session.get(JobAttempt, invocation.attempt_id).last_heartbeat_at
    clock.now += timedelta(seconds=10)
    assert run_fake_worker(reservation_engine, invocation).status == "permission_denied"
    other = invocation.model_copy(update={"invocation_id": uuid4()})
    assert run_fake_worker(reservation_engine, other).status == "permission_denied"
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, invocation.attempt_id)
        assert attempt.invocation_id == invocation.invocation_id
        assert attempt.last_heartbeat_at == first_contact


def test_permission_error_never_proceeds_to_heartbeat(
    reservation_engine, invocation, monkeypatch
):
    def fail(*args, **kwargs):
        raise RuntimeError("grant acknowledgement unavailable")

    report = Mock()
    monkeypatch.setattr(reservations, "authorize_execution", fail)
    monkeypatch.setattr(reservations, "record_heartbeat", report)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        run_fake_worker(reservation_engine, invocation)
    report.assert_not_called()


def test_heartbeat_denial_does_not_claim_success(
    reservation_engine, invocation, monkeypatch
):
    monkeypatch.setattr(reservations, "record_heartbeat", lambda *args, **kwargs: False)
    assert (
        run_fake_worker(reservation_engine, invocation).status == "heartbeat_rejected"
    )
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, invocation.attempt_id)
        assert attempt.phase == "running" and attempt.last_heartbeat_at is None


@pytest.mark.parametrize(
    "corruption", ["invalid_json", "oversized", "wrong_identity", "wrong_exit_code"]
)
def test_adapter_rejects_untrustworthy_reports(
    reservation_engine, invocation, monkeypatch, corruption
):
    raw = json.dumps(
        {
            "job_id": str(invocation.job_id),
            "attempt_id": str(invocation.attempt_id),
            "invocation_id": str(invocation.invocation_id),
            "worker_pid": os.getpid(),
            "status": "contact_recorded",
        }
    ).encode()
    exit_code = 0
    if corruption == "invalid_json":
        raw = b"not JSON"
    elif corruption == "oversized":
        raw = b"x" * (execution.MAX_CONTROL_MESSAGE_BYTES + 1)
    elif corruption == "wrong_identity":
        body = json.loads(raw)
        body["attempt_id"] = str(uuid4())
        raw = json.dumps(body).encode()
    else:
        exit_code = 3

    def bad_report(command, **kwargs):
        assert kwargs["env"]["ISOJAM_DATABASE_PATH"] == str(
            reservation_engine.url.database
        )
        assert "ISOJAM_JWT_SECRET_KEY" not in kwargs["env"]
        kwargs["stdout"].write(raw)
        return subprocess.CompletedProcess(command, exit_code)

    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", "do-not-pass-to-worker")
    monkeypatch.setattr(execution.subprocess, "run", bad_report)
    with pytest.raises(WorkerLaunchError):
        LocalFakeWorkerAdapter(reservation_engine).submit(invocation)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--once"],
        ["--once", "--adapter", "local-fake", "--worker-timeout-seconds", "0"],
    ],
)
def test_dispatcher_help_and_invalid_arguments_do_not_initialize_database(
    tmp_path, monkeypatch, arguments
):
    path = tmp_path / "not-created" / "metadata.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = subprocess.run(
        [sys.executable, "-m", "app.dispatcher", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (0 if arguments == ["--help"] else 2)
    assert not path.parent.exists()


@pytest.mark.parametrize(
    "payload", [b"{}", b"not-json", b"x" * (execution.MAX_CONTROL_MESSAGE_BYTES + 1)]
)
def test_worker_rejects_bad_request_before_opening_database(
    tmp_path, monkeypatch, payload
):
    path = tmp_path / "not-created" / "metadata.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = subprocess.run(
        [sys.executable, "-m", "app.fake_worker"],
        input=payload,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert not path.parent.exists()


@pytest.mark.parametrize("database", [":memory:", "relative.db"])
def test_local_adapter_rejects_unsupported_database_before_dispatch(database):
    engine = create_engine(URL.create("sqlite", database=database))
    try:
        with pytest.raises(ValueError, match="SQLite|absolute"):
            LocalFakeWorkerAdapter(engine)
    finally:
        engine.dispose()
