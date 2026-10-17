import json
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock, call
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.dispatcher as dispatcher
from app.db_models import Job, JobAttempt
from app.dispatcher import DispatchResult, run_dispatch_loop
from app.execution import WorkerLaunchError
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore
from tests.factories import create_test_upload, create_test_user, make_wav_bytes


def test_loop_waits_on_idle_suppresses_duplicates_and_keeps_one_owner(monkeypatch):
    results = [
        DispatchResult("idle"),
        DispatchResult("idle"),
        DispatchResult("completed", job_id=uuid4()),
        DispatchResult("idle"),
    ]
    cycle = Mock(side_effect=results)
    sleeps = Mock()
    reports = []
    monkeypatch.setattr(dispatcher, "dispatch_once", cycle)
    monkeypatch.setattr(dispatcher.time, "sleep", sleeps)
    owner = uuid4()
    run_dispatch_loop(
        Mock(),
        Mock(),
        dispatcher_id=owner,
        should_stop=lambda: cycle.call_count == 4,
        on_result=reports.append,
        poll_interval_seconds=0.2,
    )
    assert [report.status for report in reports] == ["idle", "completed", "idle"]
    assert sleeps.call_args_list == [call(0.2), call(0.2)]
    assert all(call.kwargs["dispatcher_id"] == owner for call in cycle.call_args_list)


def test_loop_retries_busy_cycles_with_wait_without_restarting(monkeypatch):
    cycle = Mock(
        side_effect=[
            reservations.ReservationBusyError("busy"),
            reservations.ReservationBusyError("busy"),
            DispatchResult("completed"),
        ]
    )
    sleeps, reports = Mock(), []
    monkeypatch.setattr(dispatcher, "dispatch_once", cycle)
    monkeypatch.setattr(dispatcher.time, "sleep", sleeps)
    run_dispatch_loop(
        Mock(),
        Mock(),
        dispatcher_id=uuid4(),
        should_stop=lambda: cycle.call_count == 3,
        on_result=reports.append,
    )
    assert [report.status for report in reports] == ["database_busy", "completed"]
    assert sleeps.call_count == 2


def test_loop_unexpected_error_exits_to_supervisor(monkeypatch):
    cycle = Mock(side_effect=RuntimeError("unexpected dispatcher error"))
    monkeypatch.setattr(dispatcher, "dispatch_once", cycle)
    with pytest.raises(RuntimeError, match="unexpected dispatcher error"):
        run_dispatch_loop(
            Mock(),
            Mock(),
            dispatcher_id=uuid4(),
            should_stop=lambda: False,
            on_result=Mock(),
        )
    assert cycle.call_count == 1


@pytest.mark.parametrize("interval", [0, -1, 61, float("nan"), float("inf"), True])
def test_invalid_interval_cannot_begin_cycle(monkeypatch, interval):
    cycle = Mock()
    monkeypatch.setattr(dispatcher, "dispatch_once", cycle)
    with pytest.raises(ValueError, match="poll interval"):
        run_dispatch_loop(
            Mock(),
            Mock(),
            dispatcher_id=uuid4(),
            should_stop=lambda: False,
            on_result=Mock(),
            poll_interval_seconds=interval,
        )
    cycle.assert_not_called()


def test_expired_reserved_slot_is_reclaimed_without_a_new_attempt(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    token = reservations.reserve_next_job(
        reservation_engine, dispatcher_id=uuid4(), reclaim_expired=True
    )
    assert token.attempt_id == owned_reservation.attempt_id
    assert token.attempt_number == 1 and token.dispatcher_generation == 2
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    assert reservations.begin_submission(reservation_engine, token)
    clock.now += timedelta(hours=2)
    assert (
        reservations.reserve_next_job(
            reservation_engine, dispatcher_id=uuid4(), reclaim_expired=True
        )
        is None
    )
    with Session(reservation_engine) as session:
        assert len(session.scalars(select(JobAttempt)).all()) == 1


def test_automatic_takeover_refuses_multiple_occupied_slots(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    with Session(reservation_engine) as session:
        job = session.get(Job, owned_reservation.job_id)
        other = Job(
            upload_id=job.upload_id, status="pending", execution_backend="queued"
        )
        session.add(other)
        session.flush()
        session.add(JobAttempt(job_id=other.id, attempt_number=1))
        session.commit()
    assert (
        reservations.reserve_next_job(
            reservation_engine, dispatcher_id=uuid4(), reclaim_expired=True
        )
        is None
    )
    with Session(reservation_engine) as session:
        assert (
            session.get(JobAttempt, owned_reservation.attempt_id).dispatcher_generation
            == 1
        )


def test_competing_takeovers_only_grant_one_new_generation(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    barrier = Barrier(2)

    def claim():
        engine = create_engine(reservation_engine.url)
        try:
            barrier.wait(timeout=5)
            return reservations.reserve_next_job(
                engine,
                dispatcher_id=uuid4(),
                reclaim_expired=True,
                busy_timeout_ms=5000,
            )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        claims = [future.result(timeout=10) for future in futures]
    grants = [token for token in claims if token is not None]
    assert len(grants) == 1 and grants[0].dispatcher_generation == 2


def test_dispatch_cycle_uses_reclaimed_generation(
    reservation_engine, owned_reservation, clock, tmp_path
):
    clock.now = owned_reservation.reservation_expires_at
    submit = Mock(side_effect=WorkerLaunchError("stop before worker launch"))
    adapter = SimpleNamespace(
        submit=submit, results=LocalResultStore(tmp_path / "results")
    )
    result = dispatcher.dispatch_once(
        reservation_engine, adapter, dispatcher_id=uuid4()
    )
    assert result.status == "submission_unresolved"
    invocation = submit.call_args.args[0]
    assert invocation.attempt_id == owned_reservation.attempt_id
    assert invocation.dispatcher_generation == 2
    assert not reservations.begin_submission(reservation_engine, owned_reservation)


def wait_until(predicate, *, process=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None:
            assert process.poll() is None, "Dispatcher exited before the expected state"
        if value := predicate():
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for dispatcher state")


@contextmanager
def service(tmp_path, *, model=False):
    args = [
        "--serve",
        "--adapter",
        "local-model" if model else "local-fake",
        "--poll-interval-seconds",
        "0.1",
    ]
    if model:
        # Use the production service/worker with a small CPU substitute for CUDA.
        code = "from tests.model_worker_helpers import service_main; raise SystemExit(service_main())"
        command = [sys.executable, "-c", code, *args]
    else:
        command = [sys.executable, "-m", "app.dispatcher", *args]
    output = tmp_path / "service.jsonl"
    error = tmp_path / "service.stderr"
    with output.open("wb") as stdout, error.open("wb") as stderr:
        process = subprocess.Popen(
            command, stdout=stdout, stderr=stderr, start_new_session=True
        )
        try:
            yield SimpleNamespace(process=process, output=output, error=error)
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def add_job(engine, *, stored_filename="song.wav", created_at=None):
    with Session(engine) as session:
        user = create_test_user(session, email=f"{uuid4()}@example.com")
        upload = create_test_upload(session, user, stored_filename=stored_filename)
        job = Job(
            upload_id=upload.id,
            status="pending",
            execution_backend="queued",
            created_at=created_at or datetime.now(UTC),
        )
        session.add(job)
        session.flush()
        job_id = job.id
        session.commit()
        return job_id


def job_status(engine, job_id):
    with Session(engine) as session:
        return session.get(Job, job_id).status


def test_service_discovers_later_jobs_and_stops_idle(reservation_engine, tmp_path):
    with service(tmp_path) as running:
        wait_until(
            lambda: '"idle"' in running.output.read_text(), process=running.process
        )
        for _ in range(2):
            job_id = add_job(reservation_engine)
            wait_until(
                lambda: job_status(reservation_engine, job_id) == "completed",
                process=running.process,
            )
        running.process.terminate()
        assert running.process.wait(timeout=5) == 0, running.error.read_text()
        reports = [json.loads(line) for line in running.output.read_text().splitlines()]
        assert sum(report["status"] == "completed" for report in reports) == 2
        assert reports[-1]["status"] == "stopped"


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_drains_current_worker_and_never_starts_next_job(
    reservation_engine, tmp_path, stop_signal
):
    from app.config import get_audio_storage_dir

    upload_dir = get_audio_storage_dir() / "uploads"
    upload_dir.mkdir(parents=True)
    (upload_dir / "song.wav").write_bytes(make_wav_bytes(frames=88200))
    first = add_job(
        reservation_engine, created_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    second = add_job(reservation_engine)
    with service(tmp_path, model=True) as running:
        wait_until(
            lambda: job_status(reservation_engine, first) == "processing",
            process=running.process,
        )
        running.process.send_signal(stop_signal)
        assert running.process.wait(timeout=20) == 0, running.error.read_text()
    assert job_status(reservation_engine, first) == "completed"
    assert job_status(reservation_engine, second) == "pending"
    with Session(reservation_engine) as session:
        attempts = session.scalars(select(JobAttempt)).all()
        assert len(attempts) == 1 and attempts[0].execution_stopped_at is not None


@pytest.mark.parametrize(
    "arguments",
    [
        ["--serve", "--once"],
        ["--serve", "--reconcile-only"],
        ["--serve", "--poll-interval-seconds", "nan"],
        ["--serve", "--poll-interval-seconds", "0"],
    ],
)
def test_invalid_service_arguments_never_open_database(
    tmp_path, monkeypatch, arguments
):
    path = tmp_path / "not-created" / "db.sqlite"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(path))
    result = subprocess.run(
        [sys.executable, "-m", "app.dispatcher", "--adapter", "local-fake", *arguments],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 2 and not path.parent.exists()
