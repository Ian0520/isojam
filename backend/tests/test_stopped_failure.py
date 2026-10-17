from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.db_models import Job, JobAttempt, JobOutput
from app.repositories import job_reservations as reservations


@pytest.fixture
def stopped_invocation(reservation_engine, owned_reservation):
    token = owned_reservation
    invocation_id, worker_id = uuid4(), uuid4()
    assert reservations.begin_submission(reservation_engine, token)
    assert reservations.authorize_execution(
        reservation_engine,
        token,
        invocation_id=invocation_id,
        local_worker_pid=1234,
        local_worker_id=worker_id,
    )
    assert reservations.record_execution_stopped(
        reservation_engine,
        token,
        invocation_id=invocation_id,
        exit_code=1,
        local_worker_pid=1234,
        local_worker_id=worker_id,
    )
    return token, invocation_id


def snapshot(engine):
    with engine.connect() as connection:
        return tuple(
            connection.execute(select(table.__table__)).all()
            for table in (Job, JobAttempt, JobOutput)
        )


def test_failure_is_atomic_idempotent_and_releases_capacity_without_retry(
    reservation_engine,
    stopped_invocation,
):
    token, invocation_id = stopped_invocation
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert reservations.fail_stopped_attempt(
        reservation_engine, token, invocation_id=invocation_id
    )
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, token.attempt_id)
        job = session.get(Job, token.job_id)
        assert attempt.phase == "failed" and job.status == "failed"
        assert attempt.finished_at >= attempt.execution_stopped_at
        assert attempt.result_manifest_key is None
        assert not session.scalars(select(JobOutput)).all()
        next_job = Job(
            upload_id=job.upload_id, status="pending", execution_backend="queued"
        )
        session.add(next_job)
        session.commit()
        next_id = next_job.id
    before = snapshot(reservation_engine)
    assert reservations.fail_stopped_attempt(
        reservation_engine, token, invocation_id=invocation_id
    )
    assert snapshot(reservation_engine) == before
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id
        == next_id
    )


@pytest.mark.parametrize(
    "damage",
    [
        "no-stop",
        "exit-zero",
        "stale-owner",
        "wrong-invocation",
        "newer-attempt",
        "outputs",
    ],
)
def test_failure_requires_current_authority_nonzero_stop_and_no_publication(
    reservation_engine,
    stopped_invocation,
    damage,
):
    token, invocation_id = stopped_invocation
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, token.attempt_id)
        if damage == "no-stop":
            attempt.execution_stopped_at = attempt.execution_exit_code = None
        elif damage == "exit-zero":
            attempt.execution_exit_code = 0
        elif damage == "stale-owner":
            token = replace(
                token, dispatcher_generation=token.dispatcher_generation + 1
            )
        elif damage == "wrong-invocation":
            invocation_id = uuid4()
        elif damage == "newer-attempt":
            session.add(JobAttempt(job_id=token.job_id, attempt_number=2))
        else:
            session.add(
                JobOutput(job_id=token.job_id, stem="guitar", path="/existing.wav")
            )
        session.commit()
    before = snapshot(reservation_engine)
    assert not reservations.fail_stopped_attempt(
        reservation_engine, token, invocation_id=invocation_id
    )
    assert snapshot(reservation_engine) == before


def test_failed_failure_commit_rolls_back_both_states(
    reservation_engine, stopped_invocation
):
    token, invocation_id = stopped_invocation
    before = snapshot(reservation_engine)

    def fail_commit(connection):
        raise RuntimeError("failure commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="failure commit failed"):
            reservations.fail_stopped_attempt(
                reservation_engine, token, invocation_id=invocation_id
            )
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    assert snapshot(reservation_engine) == before
    assert reservations.fail_stopped_attempt(
        reservation_engine, token, invocation_id=invocation_id
    )


def test_competing_controllers_acknowledge_one_failure(
    reservation_engine, stopped_invocation
):
    token, invocation_id = stopped_invocation
    barrier = Barrier(2)

    def finalize():
        engine = create_engine(reservation_engine.url)
        try:
            barrier.wait(timeout=5)
            return reservations.fail_stopped_attempt(
                engine, token, invocation_id=invocation_id, busy_timeout_ms=5000
            )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(finalize) for _ in range(2)]
        assert [future.result(timeout=10) for future in futures] == [True, True]
    with Session(reservation_engine) as session:
        assert session.get(Job, token.job_id).status == "failed"
        assert len(session.scalars(select(JobAttempt)).all()) == 1
