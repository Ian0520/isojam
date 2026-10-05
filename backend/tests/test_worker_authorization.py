import multiprocessing
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt
from app.repositories import job_reservations as reservations


@pytest.fixture
def submission_reservation(reservation_engine, owned_reservation):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    return owned_reservation


def execution_state(engine, reservation):
    with Session(engine) as session:
        return (
            session.get(JobAttempt, reservation.attempt_id),
            session.get(Job, reservation.job_id),
        )


def test_submission_commits_independent_deadline_once(
    reservation_engine, owned_reservation, clock
):
    clock.now += timedelta(seconds=1)
    assert reservations.begin_submission(
        reservation_engine, owned_reservation, authorization_ttl_seconds=120
    )
    attempt, job = execution_state(reservation_engine, owned_reservation)
    deadline = clock.now + timedelta(seconds=120)
    assert attempt.execution_authorization_expires_at == deadline
    assert attempt.invocation_id is None
    assert attempt.started_at is None
    assert job.status == "pending"
    clock.now += timedelta(seconds=10)
    assert not reservations.begin_submission(
        reservation_engine, owned_reservation, authorization_ttl_seconds=3600
    )
    assert (
        execution_state(reservation_engine, owned_reservation)[
            0
        ].execution_authorization_expires_at
        == deadline
    )


@pytest.mark.parametrize("phase", ["submitting", "submitted", "uncertain"])
def test_worker_can_receive_permission_before_or_after_provider_ack(
    reservation_engine, submission_reservation, clock, phase
):
    with Session(reservation_engine) as session:
        session.get(JobAttempt, submission_reservation.attempt_id).phase = phase
        session.commit()
    invocation = uuid4()
    clock.now += timedelta(seconds=1)
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=invocation
    )
    attempt, job = execution_state(reservation_engine, submission_reservation)
    assert attempt.phase == "running"
    assert attempt.invocation_id == invocation
    assert attempt.started_at == attempt.updated_at == clock.now
    assert attempt.finished_at is None
    assert job.status == "processing"
    assert job.updated_at == clock.now


def test_duplicate_and_other_invocations_cannot_receive_another_grant(
    reservation_engine, submission_reservation, clock
):
    invocation = uuid4()
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=invocation
    )
    attempt, job = execution_state(reservation_engine, submission_reservation)
    started = attempt.started_at
    clock.now += timedelta(seconds=1)
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=invocation
    )
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )
    attempt, job = execution_state(reservation_engine, submission_reservation)
    assert attempt.invocation_id == invocation
    assert attempt.started_at == started
    assert attempt.updated_at == started
    assert job.status == "processing"


def test_permission_uses_submission_deadline_not_reservation_expiry(
    reservation_engine, submission_reservation, clock
):
    clock.now = submission_reservation.reservation_expires_at + timedelta(seconds=1)
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )


@pytest.mark.parametrize("offset", [timedelta(), timedelta(seconds=1)])
def test_expired_authorization_denies_permission_without_releasing_capacity(
    reservation_engine, submission_reservation, clock, offset
):
    attempt, job = execution_state(reservation_engine, submission_reservation)
    clock.now = attempt.execution_authorization_expires_at + offset
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )
    attempt, job = execution_state(reservation_engine, submission_reservation)
    assert attempt.phase == "submitting"
    assert attempt.invocation_id is None
    assert attempt.started_at is None
    assert job.status == "pending"
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert (
        reservations.reclaim_reservation(
            reservation_engine, submission_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )


def test_expiry_cannot_replace_an_already_authorized_invocation(
    reservation_engine, submission_reservation, clock
):
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )
    clock.now += timedelta(days=1)
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert (
        reservations.reclaim_reservation(
            reservation_engine, submission_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )
    assert (
        execution_state(reservation_engine, submission_reservation)[1].status
        == "processing"
    )


def test_permission_without_submission_intent_is_denied(
    reservation_engine, owned_reservation
):
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=uuid4()
    )
    assert execution_state(reservation_engine, owned_reservation)[0].phase == "reserved"


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "attempt_id",
        "dispatcher_id",
        "attempt_number",
        "dispatcher_generation",
    ],
)
def test_mismatched_authority_cannot_execute(
    reservation_engine, submission_reservation, field
):
    value = uuid4() if field.endswith("_id") else 2
    assert not reservations.authorize_execution(
        reservation_engine,
        replace(submission_reservation, **{field: value}),
        invocation_id=uuid4(),
    )
    assert (
        execution_state(reservation_engine, submission_reservation)[0].invocation_id
        is None
    )


def test_previous_owner_cannot_authorize_after_takeover(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    reclaimed = reservations.reclaim_reservation(
        reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
    )
    assert reservations.begin_submission(reservation_engine, reclaimed)
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=uuid4()
    )
    assert reservations.authorize_execution(
        reservation_engine, reclaimed, invocation_id=uuid4()
    )


@pytest.mark.parametrize(
    "change",
    [
        "local",
        "processing",
        "completed",
        "failed",
        "newer_attempt",
        "started",
        "finished",
        "missing_deadline",
    ],
)
def test_ineligible_execution_cannot_change_attempt_or_job(
    reservation_engine, submission_reservation, clock, change
):
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, submission_reservation.attempt_id)
        job = session.get(Job, submission_reservation.job_id)
        if change == "local":
            job.execution_backend = "local"
        elif change == "newer_attempt":
            session.add(JobAttempt(job_id=job.id, attempt_number=2, phase="failed"))
        elif change == "started":
            attempt.started_at = clock.now
        elif change == "finished":
            attempt.finished_at = clock.now
        elif change == "missing_deadline":
            attempt.execution_authorization_expires_at = None
        else:
            job.status = change
        session.commit()
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )
    assert (
        execution_state(reservation_engine, submission_reservation)[0].invocation_id
        is None
    )


@pytest.mark.parametrize(
    "phase", ["reserved", "running", "result_ready", "succeeded", "failed"]
)
def test_other_phases_cannot_receive_new_execution_permission(
    reservation_engine, submission_reservation, phase
):
    with Session(reservation_engine) as session:
        session.get(JobAttempt, submission_reservation.attempt_id).phase = phase
        session.commit()
    assert not reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )


@pytest.mark.parametrize("failure_at", ["job_update", "commit"])
def test_failure_rolls_back_attempt_and_public_job_together(
    reservation_engine, submission_reservation, failure_at
):
    def fail_commit(connection):
        raise RuntimeError("authorization failed")

    def fail_job_update(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("UPDATE jobs"):
            raise RuntimeError("authorization failed")

    listener = fail_commit if failure_at == "commit" else fail_job_update
    event_name = "commit" if failure_at == "commit" else "before_cursor_execute"
    event.listen(reservation_engine, event_name, listener)
    try:
        with pytest.raises(RuntimeError, match="authorization failed"):
            reservations.authorize_execution(
                reservation_engine, submission_reservation, invocation_id=uuid4()
            )
    finally:
        event.remove(reservation_engine, event_name, listener)
    attempt, job = execution_state(reservation_engine, submission_reservation)
    assert attempt.phase == "submitting"
    assert attempt.invocation_id is None
    assert attempt.started_at is None
    assert job.status == "pending"
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=uuid4()
    )


@pytest.mark.parametrize("lock_at", ["begin", "commit"])
def test_real_sqlite_contention_does_not_leak_execution_permission(
    reservation_engine, submission_reservation, lock_at
):
    locker_engine = create_engine(reservation_engine.url)
    try:
        with locker_engine.connect() as locker:
            if lock_at == "begin":
                locker.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                locker.exec_driver_sql("BEGIN")
                locker.exec_driver_sql("SELECT id FROM jobs").all()
            with pytest.raises(reservations.ReservationBusyError) as caught:
                reservations.authorize_execution(
                    reservation_engine,
                    submission_reservation,
                    invocation_id=uuid4(),
                    busy_timeout_ms=25,
                )
            assert isinstance(caught.value.__cause__, OperationalError)
            locker.rollback()
        attempt, job = execution_state(reservation_engine, submission_reservation)
        assert attempt.phase == "submitting"
        assert attempt.invocation_id is None
        assert job.status == "pending"
        assert reservations.authorize_execution(
            reservation_engine, submission_reservation, invocation_id=uuid4()
        )
    finally:
        locker_engine.dispose()


def test_permission_deadline_is_checked_after_acquiring_write_lock(
    reservation_engine, submission_reservation, clock
):
    deadline = execution_state(reservation_engine, submission_reservation)[
        0
    ].execution_authorization_expires_at

    def cross_deadline(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            clock.now = deadline

    event.listen(reservation_engine, "after_cursor_execute", cross_deadline)
    try:
        assert not reservations.authorize_execution(
            reservation_engine, submission_reservation, invocation_id=uuid4()
        )
    finally:
        event.remove(reservation_engine, "after_cursor_execute", cross_deadline)


def execution_in_process(
    database_url, reservation, invocation_id, now, barrier, results
):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    reservations._utc_now = lambda: now

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            barrier.wait(timeout=15)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        granted = reservations.authorize_execution(
            engine, reservation, invocation_id=invocation_id, busy_timeout_ms=3000
        )
        results.put((str(invocation_id), granted))
    except Exception as error:
        results.put(("error", repr(error)))
    finally:
        engine.dispose()


@pytest.mark.parametrize("same_invocation", [False, True])
def test_competing_worker_processes_receive_only_one_grant(
    reservation_engine, submission_reservation, clock, same_invocation
):
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    invocation_ids = [uuid4(), uuid4()]
    if same_invocation:
        invocation_ids[1] = invocation_ids[0]
    processes = [
        context.Process(
            target=execution_in_process,
            args=(
                str(reservation_engine.url),
                submission_reservation,
                invocation,
                clock.now,
                barrier,
                results,
            ),
        )
        for invocation in invocation_ids
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert all(invocation != "error" for invocation, result in outcomes), outcomes
        assert sum(result for invocation, result in outcomes) == 1, outcomes
        winner = next(invocation for invocation, result in outcomes if result)
        attempt, job = execution_state(reservation_engine, submission_reservation)
        assert str(attempt.invocation_id) == winner
        assert attempt.phase == "running"
        assert job.status == "processing"
        restarted = create_engine(reservation_engine.url)
        try:
            assert not reservations.authorize_execution(
                restarted, submission_reservation, invocation_id=uuid4()
            )
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


def interrupted_execution(database_url, reservation, inserted, release):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    # Keep the simulated control clock within the test submission deadline.
    reservations._utc_now = lambda: reservation.reservation_expires_at

    def pause_after_update(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("UPDATE job_attempts"):
            inserted.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Parent did not terminate child")

    event.listen(engine, "after_cursor_execute", pause_after_update)
    try:
        reservations.authorize_execution(engine, reservation, invocation_id=uuid4())
    finally:
        engine.dispose()


def test_process_death_before_commit_leaves_no_invocation_or_processing_job(
    reservation_engine, submission_reservation
):
    context = multiprocessing.get_context("spawn")
    updated, release = context.Event(), context.Event()
    process = context.Process(
        target=interrupted_execution,
        args=(str(reservation_engine.url), submission_reservation, updated, release),
    )
    try:
        process.start()
        assert updated.wait(timeout=15)
        process.terminate()
        process.join(timeout=5)
        assert not process.is_alive()
        attempt, job = execution_state(reservation_engine, submission_reservation)
        assert attempt.phase == "submitting"
        assert attempt.invocation_id is None
        assert job.status == "pending"
        assert reservations.authorize_execution(
            reservation_engine, submission_reservation, invocation_id=uuid4()
        )
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=5)


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5])
def test_invalid_authorization_lifetime_is_rejected_before_intent(
    reservation_engine, owned_reservation, ttl
):
    with pytest.raises(ValueError, match="authorization_ttl_seconds"):
        reservations.begin_submission(
            reservation_engine, owned_reservation, authorization_ttl_seconds=ttl
        )
    assert execution_state(reservation_engine, owned_reservation)[0].phase == "reserved"


@pytest.mark.parametrize("invocation_id", [None, "worker-1", True])
def test_invocation_identity_requires_uuid(
    reservation_engine, submission_reservation, invocation_id
):
    with pytest.raises(ValueError, match="invocation_id"):
        reservations.authorize_execution(
            reservation_engine, submission_reservation, invocation_id=invocation_id
        )


@pytest.mark.parametrize("missing", ["deadline", "start", "owner", "running_phase"])
def test_database_rejects_incomplete_execution_authority(
    reservation_engine, owned_reservation, clock, missing
):
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.invocation_id = uuid4()
        attempt.started_at = clock.now
        attempt.execution_authorization_expires_at = clock.now + timedelta(seconds=60)
        attempt.phase = "running"
        if missing == "deadline":
            attempt.execution_authorization_expires_at = None
        elif missing == "start":
            attempt.started_at = None
        elif missing == "running_phase":
            attempt.phase = "submitted"
        else:
            attempt.dispatcher_id = None
            attempt.dispatcher_generation = 0
            attempt.reservation_expires_at = None
        with pytest.raises(IntegrityError, match="ck_job_attempts_execution_authority"):
            session.commit()


def test_invocation_cannot_be_reused_for_another_attempt(
    reservation_engine, submission_reservation, clock
):
    invocation = uuid4()
    assert reservations.authorize_execution(
        reservation_engine, submission_reservation, invocation_id=invocation
    )
    # Simulate a separately verified terminal outcome; this stage exposes no
    # terminal/retry operation and does not treat a worker exit as that proof.
    with Session(reservation_engine) as session:
        previous = session.get(JobAttempt, submission_reservation.attempt_id)
        previous.phase = "failed"
        previous.finished_at = clock.now
        first_job = session.get(Job, submission_reservation.job_id)
        first_job.status = "failed"
        second = Job(
            upload_id=first_job.upload_id, status="pending", execution_backend="queued"
        )
        session.add(second)
        session.commit()
    reservation = reservations.reserve_next_job(
        reservation_engine, dispatcher_id=uuid4()
    )
    assert reservations.begin_submission(reservation_engine, reservation)
    assert not reservations.authorize_execution(
        reservation_engine, reservation, invocation_id=invocation
    )
    assert reservations.authorize_execution(
        reservation_engine, reservation, invocation_id=uuid4()
    )
    # The database itself rejects duplication even outside the repository.
    with Session(reservation_engine) as session:
        session.get(JobAttempt, reservation.attempt_id).invocation_id = invocation
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
            session.commit()
