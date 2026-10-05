import multiprocessing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt
from app.repositories import job_reservations as reservations


def stored_attempt(engine, reservation):
    with Session(engine) as session:
        return session.get(JobAttempt, reservation.attempt_id)


def test_reservation_persists_owner_generation_and_utc_expiry(
    reservation_engine, owned_reservation, clock
):
    stored = stored_attempt(reservation_engine, owned_reservation)
    assert stored.dispatcher_id == owned_reservation.dispatcher_id
    assert stored.dispatcher_generation == owned_reservation.dispatcher_generation == 1
    assert stored.reservation_expires_at == clock.now + timedelta(seconds=60)
    assert stored.reservation_expires_at == owned_reservation.reservation_expires_at
    assert stored.reservation_expires_at.tzinfo is UTC


def test_submission_intent_commits_once_without_starting_job(
    reservation_engine, owned_reservation, clock
):
    clock.now += timedelta(seconds=1)
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    stored = stored_attempt(reservation_engine, owned_reservation)
    assert stored.phase == "submitting"
    assert stored.updated_at == clock.now
    assert stored.started_at is None
    with Session(reservation_engine) as session:
        assert session.get(Job, owned_reservation.job_id).status == "pending"


def test_reclaim_keeps_attempt_and_slot_but_fences_previous_generation(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    owner = uuid4()
    reclaimed = reservations.reclaim_reservation(
        reservation_engine,
        owned_reservation.attempt_id,
        dispatcher_id=owner,
        reservation_ttl_seconds=120,
    )
    assert reclaimed.job_id == owned_reservation.job_id
    assert reclaimed.attempt_id == owned_reservation.attempt_id
    assert reclaimed.attempt_number == 1
    assert reclaimed.dispatcher_id == owner
    assert reclaimed.dispatcher_generation == 2
    assert reclaimed.reservation_expires_at == clock.now + timedelta(seconds=120)
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    with Session(reservation_engine) as session:
        assert len(session.scalars(select(JobAttempt)).all()) == 1
    # Even guessing the current owner cannot make the old generation valid.
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    assert not reservations.begin_submission(
        reservation_engine, replace(owned_reservation, dispatcher_id=owner)
    )
    assert reservations.begin_submission(reservation_engine, reclaimed)


def test_same_owner_reclaim_still_invalidates_old_token(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    reclaimed = reservations.reclaim_reservation(
        reservation_engine,
        owned_reservation.attempt_id,
        dispatcher_id=owned_reservation.dispatcher_id,
    )
    assert reclaimed.dispatcher_generation == 2
    # Move the clock back to prove generation, rather than expiry, rejects it.
    clock.now -= timedelta(seconds=1)
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    assert reservations.begin_submission(reservation_engine, reclaimed)


def test_unexpired_reservation_cannot_be_reclaimed(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at - timedelta(microseconds=1)
    assert (
        reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )
    assert (
        stored_attempt(reservation_engine, owned_reservation).dispatcher_generation == 1
    )


def test_exact_expiry_denies_submission_even_with_forged_future_expiry(
    reservation_engine, owned_reservation, clock
):
    clock.now = owned_reservation.reservation_expires_at
    token = replace(
        owned_reservation, reservation_expires_at=clock.now + timedelta(days=1)
    )
    assert not reservations.begin_submission(reservation_engine, token)
    assert stored_attempt(reservation_engine, token).phase == "reserved"


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
def test_wrong_authority_identifiers_cannot_submit(
    reservation_engine, owned_reservation, field
):
    value = uuid4() if field.endswith("_id") else 2
    assert not reservations.begin_submission(
        reservation_engine, replace(owned_reservation, **{field: value})
    )
    assert stored_attempt(reservation_engine, owned_reservation).phase == "reserved"


@pytest.mark.parametrize(
    "phase",
    [
        "submitting",
        "submitted",
        "running",
        "result_ready",
        "uncertain",
        "succeeded",
        "failed",
    ],
)
def test_later_phase_never_allows_reclaim_or_repeated_submission(
    reservation_engine, owned_reservation, clock, phase
):
    with Session(reservation_engine) as session:
        session.get(JobAttempt, owned_reservation.attempt_id).phase = phase
        session.commit()
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    clock.now += timedelta(days=1)
    assert (
        reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )
    assert stored_attempt(reservation_engine, owned_reservation).phase == phase


@pytest.mark.parametrize(
    "change", ["local", "processing", "completed", "failed", "newer_attempt"]
)
def test_ineligible_or_superseded_job_cannot_submit_or_reclaim(
    reservation_engine, owned_reservation, clock, change
):
    with Session(reservation_engine) as session:
        job = session.get(Job, owned_reservation.job_id)
        if change == "local":
            job.execution_backend = "local"
        elif change == "newer_attempt":
            session.add(JobAttempt(job_id=job.id, attempt_number=2, phase="failed"))
        else:
            job.status = change
        session.commit()
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    clock.now = owned_reservation.reservation_expires_at
    assert (
        reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )


def test_unowned_attempt_stays_blocked_without_invented_authority(
    reservation_engine, owned_reservation, clock
):
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.dispatcher_id = None
        attempt.dispatcher_generation = 0
        attempt.reservation_expires_at = None
        session.commit()
    clock.now += timedelta(days=1)
    assert not reservations.begin_submission(reservation_engine, owned_reservation)
    assert (
        reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )
        is None
    )
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )


@pytest.mark.parametrize("operation", ["submit", "reclaim"])
def test_failed_authority_commit_rolls_back_and_cannot_grant_permission(
    reservation_engine, owned_reservation, clock, operation
):
    if operation == "reclaim":
        clock.now = owned_reservation.reservation_expires_at

    def perform():
        if operation == "submit":
            return reservations.begin_submission(reservation_engine, owned_reservation)
        return reservations.reclaim_reservation(
            reservation_engine, owned_reservation.attempt_id, dispatcher_id=uuid4()
        )

    def fail_commit(connection):
        raise RuntimeError("commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            perform()
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    stored = stored_attempt(reservation_engine, owned_reservation)
    assert stored.phase == "reserved"
    assert stored.dispatcher_generation == 1
    assert stored.dispatcher_id == owned_reservation.dispatcher_id
    assert perform()


@pytest.mark.parametrize("operation", ["submit", "reclaim"])
def test_time_is_sampled_after_lock_acquisition(
    reservation_engine, owned_reservation, clock, operation
):
    # Advance the control clock when BEGIN finishes, simulating a lock wait
    # across expiry without a flaky wall-clock sleep.
    def cross_expiry(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            clock.now = owned_reservation.reservation_expires_at

    event.listen(reservation_engine, "after_cursor_execute", cross_expiry)
    try:
        if operation == "submit":
            assert not reservations.begin_submission(
                reservation_engine, owned_reservation
            )
        else:
            assert (
                reservations.reclaim_reservation(
                    reservation_engine,
                    owned_reservation.attempt_id,
                    dispatcher_id=uuid4(),
                )
                is not None
            )
    finally:
        event.remove(reservation_engine, "after_cursor_execute", cross_expiry)


def authority_in_process(
    database_url, token, operation, now, reached_begin, acquired, release, results
):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    reservations._utc_now = lambda: now

    def before_begin(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            reached_begin.set()

    def after_begin(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            acquired.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Parent did not release transaction")

    event.listen(engine, "before_cursor_execute", before_begin)
    event.listen(engine, "after_cursor_execute", after_begin)
    try:
        if operation == "submit":
            result = reservations.begin_submission(engine, token, busy_timeout_ms=5000)
        else:
            result = reservations.reclaim_reservation(
                engine, token.attempt_id, dispatcher_id=uuid4(), busy_timeout_ms=5000
            )
        results.put((operation, result))
    except Exception as error:
        results.put(("error", repr(error)))
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "first_operation", ["submit", "reclaim", "reclaim_twice", "submit_twice"]
)
def test_competing_processes_cannot_both_obtain_authority(
    reservation_engine, owned_reservation, first_operation
):
    # Force both transaction orders across the expiry boundary, plus duplicate
    # submissions and duplicate takeovers. Each child has an independent engine.
    first = "submit" if first_operation.startswith("submit") else "reclaim"
    second = (
        first
        if first_operation.endswith("twice")
        else ("reclaim" if first == "submit" else "submit")
    )
    expiry = owned_reservation.reservation_expires_at
    first_time = expiry - timedelta(seconds=1) if first == "submit" else expiry
    second_time = first_time if first == second else expiry
    context = multiprocessing.get_context("spawn")
    reached = [context.Event(), context.Event()]
    acquired = [context.Event(), context.Event()]
    releases = [context.Event(), context.Event()]
    results = context.Queue()
    processes = [
        context.Process(
            target=authority_in_process,
            args=(
                str(reservation_engine.url),
                owned_reservation,
                operation,
                now,
                reached[index],
                acquired[index],
                releases[index],
                results,
            ),
        )
        for index, (operation, now) in enumerate(
            [(first, first_time), (second, second_time)]
        )
    ]
    try:
        processes[0].start()
        assert acquired[0].wait(timeout=15)
        processes[1].start()
        assert reached[1].wait(timeout=15)
        releases[1].set()
        releases[0].set()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert all(operation != "error" for operation, result in outcomes), outcomes
        assert sum(bool(result) for operation, result in outcomes) == 1, outcomes
        stored = stored_attempt(reservation_engine, owned_reservation)
        assert stored.phase == ("submitting" if first == "submit" else "reserved")
        assert stored.dispatcher_generation == (1 if first == "submit" else 2)
        with Session(reservation_engine) as session:
            assert len(session.scalars(select(JobAttempt)).all()) == 1
        # A restarted control process reconstructs authority from the database.
        restarted = create_engine(reservation_engine.url)
        try:
            assert (
                reservations.reserve_next_job(restarted, dispatcher_id=uuid4()) is None
            )
            assert not reservations.begin_submission(restarted, owned_reservation)
        finally:
            restarted.dispose()
    finally:
        for release in releases:
            release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.parametrize("ttl", [0, -1, 3601, True, 1.5])
def test_invalid_reservation_lifetime_is_rejected(
    reservation_engine, owned_reservation, ttl
):
    with pytest.raises(ValueError, match="reservation_ttl_seconds"):
        reservations.reserve_next_job(
            reservation_engine, dispatcher_id=uuid4(), reservation_ttl_seconds=ttl
        )
    with pytest.raises(ValueError, match="reservation_ttl_seconds"):
        reservations.reclaim_reservation(
            reservation_engine,
            owned_reservation.attempt_id,
            dispatcher_id=uuid4(),
            reservation_ttl_seconds=ttl,
        )


@pytest.mark.parametrize(
    "values",
    [
        {"dispatcher_id": uuid4()},
        {"dispatcher_generation": 1},
        {"reservation_expires_at": datetime(2026, 1, 1, tzinfo=UTC)},
        {
            "dispatcher_id": uuid4(),
            "dispatcher_generation": -1,
            "reservation_expires_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
        {
            "dispatcher_id": uuid4(),
            "dispatcher_generation": 0,
            "reservation_expires_at": datetime(2026, 1, 1, tzinfo=UTC),
        },
    ],
)
def test_partial_or_invalid_authority_is_rejected_by_database(
    reservation_engine, owned_reservation, values
):
    with Session(reservation_engine) as session:
        session.add(
            JobAttempt(job_id=owned_reservation.job_id, attempt_number=2, **values)
        )
        with pytest.raises(
            IntegrityError, match="ck_job_attempts_dispatcher_authority"
        ):
            session.commit()


@pytest.mark.parametrize("operation", ["submit", "reclaim"])
@pytest.mark.parametrize("lock_at", ["begin", "commit"])
def test_real_sqlite_contention_cannot_grant_or_leak_authority(
    reservation_engine, owned_reservation, clock, operation, lock_at
):
    if operation == "reclaim":
        clock.now = owned_reservation.reservation_expires_at

    def perform():
        if operation == "submit":
            return reservations.begin_submission(
                reservation_engine, owned_reservation, busy_timeout_ms=25
            )
        return reservations.reclaim_reservation(
            reservation_engine,
            owned_reservation.attempt_id,
            dispatcher_id=uuid4(),
            busy_timeout_ms=25,
        )

    locker_engine = create_engine(reservation_engine.url)
    try:
        with locker_engine.connect() as locker:
            if lock_at == "begin":
                locker.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                locker.exec_driver_sql("BEGIN")
                locker.exec_driver_sql("SELECT id FROM job_attempts").all()
            with pytest.raises(reservations.ReservationBusyError) as caught:
                perform()
            assert isinstance(caught.value.__cause__, OperationalError)
            locker.rollback()
        stored = stored_attempt(reservation_engine, owned_reservation)
        assert stored.phase == "reserved"
        assert stored.dispatcher_generation == 1
        assert stored.dispatcher_id == owned_reservation.dispatcher_id
        with reservation_engine.connect() as connection:
            assert not connection.connection.dbapi_connection.in_transaction
        assert perform()
    finally:
        locker_engine.dispose()


@pytest.mark.parametrize("ttl", [1, 3600])
def test_configurable_reservation_lifetime_bounds(
    reservation_engine, owned_reservation, clock, ttl
):
    clock.now = owned_reservation.reservation_expires_at
    result = reservations.reclaim_reservation(
        reservation_engine,
        owned_reservation.attempt_id,
        dispatcher_id=uuid4(),
        reservation_ttl_seconds=ttl,
    )
    assert result.reservation_expires_at == clock.now + timedelta(seconds=ttl)


def test_missing_attempt_cannot_be_reclaimed(reservation_engine):
    assert (
        reservations.reclaim_reservation(
            reservation_engine, uuid4(), dispatcher_id=uuid4()
        )
        is None
    )


def test_dispatcher_identity_requires_uuid(reservation_engine, owned_reservation):
    with pytest.raises(ValueError, match="dispatcher_id"):
        reservations.reserve_next_job(reservation_engine, dispatcher_id="dispatcher-a")
    with pytest.raises(ValueError, match="dispatcher_id"):
        reservations.reclaim_reservation(
            reservation_engine,
            owned_reservation.attempt_id,
            dispatcher_id="dispatcher-a",
        )
    with pytest.raises(ValueError, match="dispatcher_id"):
        reservations.begin_submission(
            reservation_engine, replace(owned_reservation, dispatcher_id="dispatcher-a")
        )
