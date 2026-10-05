import multiprocessing
from dataclasses import replace
from datetime import UTC, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt
from app.repositories import job_reservations as reservations


@pytest.fixture
def running_invocation(reservation_engine, owned_reservation):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    invocation = uuid4()
    assert reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=invocation
    )
    return invocation


def state(engine, reservation):
    with Session(engine) as session:
        return session.get(JobAttempt, reservation.attempt_id), session.get(
            Job, reservation.job_id
        )


def attributes(record):
    return {
        column.name: getattr(record, column.name) for column in record.__table__.columns
    }


def test_heartbeat_records_control_time_without_changing_authority_or_job(
    reservation_engine, owned_reservation, running_invocation, clock
):
    attempt, job = state(reservation_engine, owned_reservation)
    assert attempt.last_heartbeat_at is None
    before_attempt, before_job = attributes(attempt), attributes(job)
    clock.now += timedelta(seconds=5)
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    attempt, job = state(reservation_engine, owned_reservation)
    assert attempt.last_heartbeat_at == attempt.updated_at == clock.now
    assert attempt.last_heartbeat_at.tzinfo is UTC
    after = attributes(attempt)
    for name in ("last_heartbeat_at", "updated_at"):
        before_attempt.pop(name)
        after.pop(name)
    assert after == before_attempt
    assert attributes(job) == before_job


def test_repeated_reports_are_acknowledged_without_another_execution_grant(
    reservation_engine, owned_reservation, running_invocation, clock
):
    for offset in [1, 0, 2]:
        clock.now += timedelta(seconds=offset)
        assert reservations.record_heartbeat(
            reservation_engine, owned_reservation, invocation_id=running_invocation
        )
        assert (
            state(reservation_engine, owned_reservation)[0].last_heartbeat_at
            == clock.now
        )
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=uuid4()
    )


def test_heartbeat_after_authorization_expiry_does_not_renew_or_free_slot(
    reservation_engine, owned_reservation, running_invocation, clock
):
    attempt, job = state(reservation_engine, owned_reservation)
    deadline = attempt.execution_authorization_expires_at
    clock.now += timedelta(days=1)
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    attempt, job = state(reservation_engine, owned_reservation)
    assert attempt.execution_authorization_expires_at == deadline
    assert attempt.reservation_expires_at == owned_reservation.reservation_expires_at
    assert attempt.phase == "running"
    assert job.status == "processing"
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert (
        reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "attempt_id",
        "dispatcher_id",
        "attempt_number",
        "dispatcher_generation",
        "invocation_id",
    ],
)
def test_mismatched_identity_cannot_report_heartbeat(
    reservation_engine, owned_reservation, running_invocation, field
):
    token, invocation = owned_reservation, running_invocation
    if field == "invocation_id":
        invocation = uuid4()
    else:
        token = replace(token, **{field: uuid4() if field.endswith("_id") else 2})
    assert not reservations.record_heartbeat(
        reservation_engine, token, invocation_id=invocation
    )
    assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at is None


@pytest.mark.parametrize(
    "phase",
    [
        "reserved",
        "submitting",
        "submitted",
        "uncertain",
        "result_ready",
        "succeeded",
        "failed",
    ],
)
def test_non_running_attempt_cannot_report_heartbeat(
    reservation_engine, owned_reservation, running_invocation, phase
):
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.phase = phase
        if phase in {"reserved", "submitting", "submitted"}:
            attempt.invocation_id = None
            attempt.started_at = None
        session.commit()
    assert not reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at is None


@pytest.mark.parametrize(
    "change", ["local", "pending", "completed", "failed", "newer_attempt", "finished"]
)
def test_ineligible_or_superseded_job_cannot_receive_heartbeat(
    reservation_engine, owned_reservation, running_invocation, clock, change
):
    with Session(reservation_engine) as session:
        job = session.get(Job, owned_reservation.job_id)
        if change == "local":
            job.execution_backend = "local"
        elif change == "newer_attempt":
            session.add(JobAttempt(job_id=job.id, attempt_number=2, phase="failed"))
        elif change == "finished":
            session.get(
                JobAttempt, owned_reservation.attempt_id
            ).finished_at = clock.now
        else:
            job.status = change
        session.commit()
    assert not reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at is None


@pytest.mark.parametrize("already_reported", [False, True])
def test_control_clock_rollback_cannot_regress_heartbeat_or_update_time(
    reservation_engine, owned_reservation, running_invocation, clock, already_reported
):
    if already_reported:
        clock.now += timedelta(seconds=10)
        assert reservations.record_heartbeat(
            reservation_engine, owned_reservation, invocation_id=running_invocation
        )
    previous = state(reservation_engine, owned_reservation)[0]
    expected = previous.last_heartbeat_at or previous.started_at
    clock.now -= timedelta(seconds=5)
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    attempt, job = state(reservation_engine, owned_reservation)
    assert attempt.last_heartbeat_at == expected
    assert attempt.updated_at == previous.updated_at


def test_report_time_is_sampled_after_acquiring_write_lock(
    reservation_engine, owned_reservation, running_invocation, clock
):
    expected = clock.now + timedelta(seconds=10)

    def advance_clock(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            clock.now = expected

    event.listen(reservation_engine, "after_cursor_execute", advance_clock)
    try:
        assert reservations.record_heartbeat(
            reservation_engine, owned_reservation, invocation_id=running_invocation
        )
    finally:
        event.remove(reservation_engine, "after_cursor_execute", advance_clock)
    assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at == expected


def test_failed_commit_does_not_record_contact(
    reservation_engine, owned_reservation, running_invocation, clock
):
    before = state(reservation_engine, owned_reservation)[0]
    clock.now += timedelta(seconds=1)

    def fail_commit(connection):
        raise RuntimeError("heartbeat commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="heartbeat commit failed"):
            reservations.record_heartbeat(
                reservation_engine, owned_reservation, invocation_id=running_invocation
            )
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    assert attributes(state(reservation_engine, owned_reservation)[0]) == attributes(
        before
    )
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )


@pytest.mark.parametrize("lock_at", ["begin", "commit"])
def test_real_contention_rolls_back_contact_and_restores_pool_settings(
    reservation_engine, owned_reservation, running_invocation, clock, lock_at
):
    with reservation_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout = 4321")
    locker_engine = create_engine(reservation_engine.url)
    clock.now += timedelta(seconds=1)
    try:
        with locker_engine.connect() as locker:
            locker.exec_driver_sql("BEGIN IMMEDIATE" if lock_at == "begin" else "BEGIN")
            if lock_at == "commit":
                locker.exec_driver_sql("SELECT id FROM job_attempts").all()
            with pytest.raises(reservations.ReservationBusyError) as caught:
                reservations.record_heartbeat(
                    reservation_engine,
                    owned_reservation,
                    invocation_id=running_invocation,
                    busy_timeout_ms=25,
                )
            assert isinstance(caught.value.__cause__, OperationalError)
            locker.rollback()
        assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at is None
        with reservation_engine.connect() as connection:
            assert not connection.connection.dbapi_connection.in_transaction
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 4321
            )
        assert reservations.record_heartbeat(
            reservation_engine, owned_reservation, invocation_id=running_invocation
        )
    finally:
        locker_engine.dispose()


def report_in_process(
    database_url, token, invocation, now, older, barrier, newer_committed, results
):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    reservations._utc_now = lambda: now

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            barrier.wait(timeout=15)
            if older and not newer_committed.wait(timeout=15):
                raise RuntimeError("Newer report did not commit")

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        accepted = reservations.record_heartbeat(
            engine, token, invocation_id=invocation
        )
        if not older:
            newer_committed.set()
        results.put(accepted)
    except Exception as error:
        results.put(("error", repr(error)))
        newer_committed.set()
    finally:
        engine.dispose()


def test_independent_process_reports_preserve_newer_contact_after_restart(
    reservation_engine, owned_reservation, running_invocation, clock
):
    context = multiprocessing.get_context("spawn")
    barrier, committed, results = context.Barrier(2), context.Event(), context.Queue()
    latest = clock.now + timedelta(seconds=20)
    processes = [
        context.Process(
            target=report_in_process,
            args=(
                str(reservation_engine.url),
                owned_reservation,
                running_invocation,
                clock.now + timedelta(seconds=10 if older else 20),
                older,
                barrier,
                committed,
                results,
            ),
        )
        for older in [False, True]
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert outcomes == [True, True], outcomes
        restarted = create_engine(reservation_engine.url)
        try:
            attempt, job = state(restarted, owned_reservation)
            assert attempt.last_heartbeat_at == attempt.updated_at == latest
            assert attempt.invocation_id == running_invocation
            assert job.status == "processing"
            assert (
                reservations.reserve_next_job(restarted, dispatcher_id=uuid4()) is None
            )
        finally:
            restarted.dispose()
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("missing", ["invocation", "start", "before_start"])
def test_database_rejects_heartbeat_without_valid_execution(
    reservation_engine, owned_reservation, running_invocation, clock, missing
):
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.last_heartbeat_at = clock.now
        if missing == "invocation":
            attempt.invocation_id = None
        elif missing == "start":
            attempt.started_at = None
        else:
            attempt.last_heartbeat_at = clock.now - timedelta(seconds=1)
        with pytest.raises(IntegrityError):
            session.commit()


def test_invalid_invocation_is_rejected_before_writing(
    reservation_engine, owned_reservation, running_invocation
):
    with pytest.raises(ValueError, match="invocation_id"):
        reservations.record_heartbeat(
            reservation_engine, owned_reservation, invocation_id="worker-1"
        )
    assert state(reservation_engine, owned_reservation)[0].last_heartbeat_at is None
