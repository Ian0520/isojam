from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db_models import Job, JobAttempt, JobOutput
from app.repositories import job_reservations as reservations


@pytest.fixture
def running_invocation(reservation_engine, owned_reservation):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    invocation_id = uuid4()
    assert reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=invocation_id
    )
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=invocation_id
    )
    return invocation_id


def state(engine, token):
    with engine.connect() as connection:
        return (
            connection.execute(
                select(JobAttempt.__table__).where(JobAttempt.id == token.attempt_id)
            ).one(),
            connection.execute(
                select(Job.__table__).where(Job.id == token.job_id)
            ).one(),
            connection.execute(select(JobOutput.__table__)).all(),
        )


def stop(engine, token, invocation_id, exit_code=0):
    return reservations.record_execution_stopped(
        engine, token, invocation_id=invocation_id, exit_code=exit_code
    )


def test_stop_is_durable_and_idempotent_but_does_not_complete_or_release_capacity(
    reservation_engine, owned_reservation, running_invocation, clock
):
    clock.now += timedelta(seconds=5)
    assert stop(reservation_engine, owned_reservation, running_invocation)
    before = state(reservation_engine, owned_reservation)
    attempt, job, outputs = before
    assert (
        attempt.execution_stopped_at == clock.now and attempt.execution_exit_code == 0
    )
    assert attempt.phase == "running" and attempt.finished_at is None
    assert job.status == "processing" and not outputs
    assert not reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    clock.now += timedelta(days=1)
    assert stop(reservation_engine, owned_reservation, running_invocation)
    assert not stop(reservation_engine, owned_reservation, running_invocation, -9)
    assert state(reservation_engine, owned_reservation) == before
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=uuid4()
    )


@pytest.mark.parametrize("exit_code", [0, 1, 255, -9, -255])
def test_actual_exit_status_is_diagnostic_and_clock_rollback_cannot_precede_contact(
    reservation_engine, owned_reservation, running_invocation, clock, exit_code
):
    clock.now += timedelta(seconds=30)
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=running_invocation
    )
    contact = clock.now
    clock.now -= timedelta(minutes=1)
    assert stop(reservation_engine, owned_reservation, running_invocation, exit_code)
    attempt, job, _ = state(reservation_engine, owned_reservation)
    assert attempt.execution_stopped_at == contact
    assert attempt.execution_exit_code == exit_code
    assert attempt.updated_at == contact and attempt.finished_at is None
    assert job.status == "processing"


@pytest.mark.parametrize("exit_code", [None, True, 1.0, "0", 256, -256])
def test_invalid_exit_status_is_rejected_without_mutation(
    reservation_engine, owned_reservation, running_invocation, exit_code
):
    before = state(reservation_engine, owned_reservation)
    with pytest.raises(ValueError, match="exit_code"):
        stop(reservation_engine, owned_reservation, running_invocation, exit_code)
    assert state(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize(
    "field",
    [
        "job_id",
        "attempt_id",
        "attempt_number",
        "dispatcher_id",
        "dispatcher_generation",
        "invocation_id",
    ],
)
def test_stale_controller_cannot_record_stop_for_another_invocation(
    reservation_engine, owned_reservation, running_invocation, field
):
    before = state(reservation_engine, owned_reservation)
    token, invocation_id = owned_reservation, running_invocation
    if field == "invocation_id":
        invocation_id = uuid4()
    else:
        value = 2 if field in {"attempt_number", "dispatcher_generation"} else uuid4()
        token = replace(token, **{field: value})
    assert not stop(reservation_engine, token, invocation_id)
    assert state(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize(
    "change", ["newer_attempt", "local_backend", "failed_job", "failed_attempt"]
)
def test_ineligible_attempt_cannot_acquire_stop_evidence(
    reservation_engine, owned_reservation, running_invocation, change
):
    with Session(reservation_engine) as session:
        if change == "newer_attempt":
            session.add(JobAttempt(job_id=owned_reservation.job_id, attempt_number=2))
        elif change == "failed_attempt":
            session.get(JobAttempt, owned_reservation.attempt_id).phase = "failed"
        else:
            job = session.get(Job, owned_reservation.job_id)
            if change == "local_backend":
                job.execution_backend = "local"
            else:
                job.status = "failed"
        session.commit()
    before = state(reservation_engine, owned_reservation)
    assert not stop(reservation_engine, owned_reservation, running_invocation)
    assert state(reservation_engine, owned_reservation) == before


def test_submission_without_an_authorized_invocation_cannot_acquire_stop_evidence(
    reservation_engine, owned_reservation
):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    before = state(reservation_engine, owned_reservation)
    assert not stop(reservation_engine, owned_reservation, uuid4(), 3)
    assert state(reservation_engine, owned_reservation) == before


def test_failed_stop_commit_leaves_no_durable_evidence_and_can_retry_only_the_record(
    reservation_engine, owned_reservation, running_invocation
):
    before = state(reservation_engine, owned_reservation)

    def fail(connection):
        raise RuntimeError("stop commit failed")

    event.listen(reservation_engine, "commit", fail)
    try:
        with pytest.raises(RuntimeError, match="stop commit failed"):
            stop(reservation_engine, owned_reservation, running_invocation)
    finally:
        event.remove(reservation_engine, "commit", fail)
    assert state(reservation_engine, owned_reservation) == before
    assert stop(reservation_engine, owned_reservation, running_invocation)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_code",
        "missing_time",
        "float_code",
        "out_of_range",
        "before_start",
        "after_finish",
    ],
)
def test_schema_rejects_partial_or_impossible_stop_evidence(
    reservation_engine, owned_reservation, running_invocation, clock, damage
):
    before = state(reservation_engine, owned_reservation)
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.execution_stopped_at = clock.now
        attempt.execution_exit_code = 0
        if damage == "missing_code":
            attempt.execution_exit_code = None
        elif damage == "missing_time":
            attempt.execution_stopped_at = None
        elif damage == "float_code":
            attempt.execution_exit_code = 1.5
        elif damage == "out_of_range":
            attempt.execution_exit_code = 256
        elif damage == "before_start":
            attempt.execution_stopped_at -= timedelta(seconds=1)
        else:
            attempt.finished_at = clock.now - timedelta(seconds=1)
        with pytest.raises(IntegrityError, match="ck_job_attempts_execution_stopped"):
            session.commit()
        session.rollback()
    assert state(reservation_engine, owned_reservation) == before


def test_local_controller_must_have_waited_for_the_authorized_process(
    reservation_engine, owned_reservation
):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    invocation_id = uuid4()
    worker_id = uuid4()
    assert reservations.authorize_execution(
        reservation_engine,
        owned_reservation,
        invocation_id=invocation_id,
        local_worker_pid=1234,
        local_worker_id=worker_id,
    )
    before = state(reservation_engine, owned_reservation)
    assert not reservations.record_execution_stopped(
        reservation_engine,
        owned_reservation,
        invocation_id=invocation_id,
        exit_code=3,
        local_worker_pid=1235,
        local_worker_id=worker_id,
    )
    assert state(reservation_engine, owned_reservation) == before
    assert not stop(reservation_engine, owned_reservation, invocation_id)
    # Same PID in a different delivery/container still cannot confirm this exit.
    assert not reservations.record_execution_stopped(
        reservation_engine,
        owned_reservation,
        invocation_id=invocation_id,
        exit_code=0,
        local_worker_pid=1234,
        local_worker_id=uuid4(),
    )
    assert state(reservation_engine, owned_reservation) == before
    assert reservations.record_execution_stopped(
        reservation_engine,
        owned_reservation,
        invocation_id=invocation_id,
        exit_code=0,
        local_worker_pid=1234,
        local_worker_id=worker_id,
    )


@pytest.mark.parametrize("pid", [0, -1, True, "1234", 1.5])
def test_invalid_local_pid_cannot_grant_execution_or_record_stop(
    reservation_engine, owned_reservation, pid
):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    before = state(reservation_engine, owned_reservation)
    with pytest.raises(ValueError, match="local_worker_pid"):
        reservations.authorize_execution(
            reservation_engine,
            owned_reservation,
            invocation_id=uuid4(),
            local_worker_pid=pid,
        )
    with pytest.raises(ValueError, match="local_worker_pid"):
        reservations.record_execution_stopped(
            reservation_engine,
            owned_reservation,
            invocation_id=uuid4(),
            exit_code=0,
            local_worker_pid=pid,
        )
    assert state(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize("damage", ["pid_only", "id_only", "negative_pid", "float_pid"])
def test_schema_requires_a_valid_local_identity_pair(
    reservation_engine, owned_reservation, running_invocation, damage
):
    before = state(reservation_engine, owned_reservation)
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.local_worker_id = uuid4()
        attempt.local_worker_pid = 1234
        if damage == "pid_only":
            attempt.local_worker_id = None
        elif damage == "id_only":
            attempt.local_worker_pid = None
        elif damage == "negative_pid":
            attempt.local_worker_pid = -1
        else:
            attempt.local_worker_pid = 1.5
        with pytest.raises(IntegrityError, match="ck_job_attempts_local_worker"):
            session.commit()
        session.rollback()
    assert state(reservation_engine, owned_reservation) == before
