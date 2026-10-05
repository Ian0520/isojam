import multiprocessing
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import UUID, uuid4

import pytest
from sqlalchemy import URL, create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt
from app.repositories.job_reservations import ReservationBusyError, reserve_next_job
from tests.factories import create_test_upload, create_test_user


def seed_job(
    engine,
    *,
    status="pending",
    backend="queued",
    created_at=None,
    job_id=None,
    email="user@example.com",
):
    with Session(engine) as session:
        user = create_test_user(session, email)
        upload = create_test_upload(session, user)
        job = Job(upload_id=upload.id, status=status, execution_backend=backend)
        if created_at is not None:
            job.created_at = created_at
        if job_id is not None:
            job.id = job_id
        session.add(job)
        session.flush()
        job_id = job.id
        session.commit()
    return job_id


def attempts(engine):
    with Session(engine) as session:
        return session.scalars(select(JobAttempt)).all()


def reserve_in_process(database_url, barrier, results):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)

    def synchronize(connection, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE":
            barrier.wait(timeout=15)

    event.listen(engine, "before_cursor_execute", synchronize)
    try:
        result = reserve_next_job(engine, dispatcher_id=uuid4(), busy_timeout_ms=2000)
        results.put(
            None if result is None else (str(result.job_id), str(result.attempt_id))
        )
    except Exception as error:
        results.put(("error", repr(error)))
    finally:
        engine.dispose()


def die_before_commit(database_url, inserted, release):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)

    def stop_after_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.lstrip().upper().startswith("INSERT INTO JOB_ATTEMPTS"):
            inserted.set()
            if not release.wait(timeout=15):
                raise RuntimeError("Parent did not terminate interrupted reservation")

    event.listen(engine, "after_cursor_execute", stop_after_insert)
    try:
        reserve_next_job(engine, dispatcher_id=uuid4())
    finally:
        engine.dispose()


def test_reservation_commits_attempt_and_leaves_job_pending(reservation_engine):
    job_id = seed_job(reservation_engine)
    reservation = reserve_next_job(reservation_engine, dispatcher_id=uuid4())
    assert reservation.job_id == job_id
    assert reservation.attempt_number == 1
    stored = attempts(reservation_engine)
    assert len(stored) == 1
    assert stored[0].id == reservation.attempt_id
    assert stored[0].job_id == job_id
    assert stored[0].phase == "reserved"
    assert stored[0].created_at.tzinfo is UTC
    assert stored[0].started_at is None
    with Session(reservation_engine) as session:
        assert session.get(Job, job_id).status == "pending"


def test_oldest_job_is_reserved_with_deterministic_id_tie_break(reservation_engine):
    oldest = datetime(2026, 1, 1, tzinfo=UTC)
    seed_job(
        reservation_engine, created_at=oldest + timedelta(seconds=1), job_id=UUID(int=1)
    )
    seed_job(
        reservation_engine,
        created_at=oldest,
        job_id=UUID(int=3),
        email="second@example.com",
    )
    expected = seed_job(
        reservation_engine,
        created_at=oldest,
        job_id=UUID(int=2),
        email="third@example.com",
    )
    assert (
        reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == expected
    )


def test_empty_queue_returns_none(reservation_engine):
    assert reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    assert attempts(reservation_engine) == []


def test_local_and_non_pending_jobs_are_excluded(reservation_engine):
    for index, (backend, status) in enumerate(
        [
            ("local", "pending"),
            ("local", "processing"),
            ("local", "completed"),
            ("local", "failed"),
            ("queued", "processing"),
            ("queued", "completed"),
            ("queued", "failed"),
        ]
    ):
        seed_job(
            reservation_engine,
            backend=backend,
            status=status,
            email=f"user{index}@example.com",
        )
    assert reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    assert attempts(reservation_engine) == []


@pytest.mark.parametrize(
    "phase",
    ["reserved", "submitting", "submitted", "running", "result_ready", "uncertain"],
)
def test_non_terminal_queued_attempt_holds_global_slot(reservation_engine, phase):
    active_job = seed_job(reservation_engine)
    seed_job(reservation_engine, email="other@example.com")
    with Session(reservation_engine) as session:
        session.add(
            JobAttempt(
                job_id=active_job,
                attempt_number=1,
                phase=phase,
                created_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
        # Even an inconsistent public status cannot release uncertain execution.
        session.get(Job, active_job).status = "failed"
        session.commit()
    assert reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    assert len(attempts(reservation_engine)) == 1


@pytest.mark.parametrize("phase", ["succeeded", "failed"])
def test_terminal_attempt_frees_slot_but_is_not_automatically_retried(
    reservation_engine, phase
):
    attempted_job = seed_job(
        reservation_engine, created_at=datetime(2000, 1, 1, tzinfo=UTC)
    )
    with Session(reservation_engine) as session:
        session.add(JobAttempt(job_id=attempted_job, attempt_number=1, phase=phase))
        session.commit()
    assert reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    fresh_job = seed_job(reservation_engine, email="other@example.com")
    assert (
        reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == fresh_job
    )
    assert len(attempts(reservation_engine)) == 2


def test_local_attempt_does_not_occupy_queued_execution_slot(reservation_engine):
    local_job = seed_job(reservation_engine, backend="local", status="processing")
    with Session(reservation_engine) as session:
        session.add(JobAttempt(job_id=local_job, attempt_number=1, phase="running"))
        session.commit()
    queued_job = seed_job(reservation_engine, email="other@example.com")
    assert (
        reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == queued_job
    )


@pytest.mark.parametrize("job_count", [1, 2])
def test_competing_processes_reserve_only_one_global_attempt(
    reservation_engine, job_count
):
    for index in range(job_count):
        seed_job(reservation_engine, email=f"user{index}@example.com")
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=reserve_in_process,
            args=(str(reservation_engine.url), barrier, results),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=20) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sum(outcome is None for outcome in outcomes) == 1
        winners = [outcome for outcome in outcomes if outcome is not None]
        assert len(winners) == 1
        assert winners[0][0] != "error", outcomes
        stored = attempts(reservation_engine)
        assert len(stored) == 1
        assert winners[0] == (str(stored[0].job_id), str(stored[0].id))
        # A fresh engine simulates a later dispatcher process restarting.
        restarted = create_engine(reservation_engine.url)
        try:
            assert reserve_next_job(restarted, dispatcher_id=uuid4()) is None
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


def test_process_death_before_commit_leaves_job_unreserved(reservation_engine):
    job_id = seed_job(reservation_engine)
    context = multiprocessing.get_context("spawn")
    inserted, release = context.Event(), context.Event()
    process = context.Process(
        target=die_before_commit, args=(str(reservation_engine.url), inserted, release)
    )
    try:
        process.start()
        assert inserted.wait(timeout=15), "Child never inserted the uncommitted attempt"
        process.terminate()
        process.join(timeout=5)
        assert not process.is_alive()
        assert attempts(reservation_engine) == []
        assert (
            reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == job_id
        )
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=5)


@pytest.mark.parametrize("lock_at", ["begin", "commit"])
def test_contention_is_bounded_rolls_back_and_restores_connection_settings(
    reservation_engine, lock_at
):
    job_id = seed_job(reservation_engine)
    with reservation_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout = 4321")
    locker_engine = create_engine(reservation_engine.url)
    try:
        with locker_engine.connect() as locker:
            if lock_at == "begin":
                locker.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                assert (
                    locker.exec_driver_sql("PRAGMA journal_mode").scalar_one()
                    == "delete"
                )
                locker.exec_driver_sql("BEGIN")
                locker.exec_driver_sql("SELECT id FROM jobs").all()
            started = monotonic()
            with pytest.raises(ReservationBusyError) as caught:
                reserve_next_job(
                    reservation_engine, dispatcher_id=uuid4(), busy_timeout_ms=25
                )
            assert monotonic() - started < 2
            assert isinstance(caught.value.__cause__, OperationalError)
            locker.rollback()
        # Read through the same pool first: a failed COMMIT must not leave its
        # uncommitted row visible on the connection returned to the pool.
        assert attempts(reservation_engine) == []
        with reservation_engine.connect() as connection:
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 4321
            )
            assert not connection.connection.dbapi_connection.in_transaction
        assert (
            reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == job_id
        )
        with reservation_engine.connect() as connection:
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 4321
            )
    finally:
        locker_engine.dispose()


def test_failed_commit_rolls_back_attempt_and_allows_retry(reservation_engine):
    job_id = seed_job(reservation_engine)

    def fail_commit(connection):
        raise RuntimeError("commit failed")

    event.listen(reservation_engine, "commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="commit failed"):
            reserve_next_job(reservation_engine, dispatcher_id=uuid4())
    finally:
        event.remove(reservation_engine, "commit", fail_commit)
    assert attempts(reservation_engine) == []
    assert reserve_next_job(reservation_engine, dispatcher_id=uuid4()).job_id == job_id


def test_missing_schema_is_not_misreported_as_contention(tmp_path):
    engine = create_engine(
        URL.create("sqlite", database=str(tmp_path / "unmigrated.db"))
    )
    try:
        with pytest.raises(OperationalError, match="no such table"):
            reserve_next_job(engine, dispatcher_id=uuid4())
    finally:
        engine.dispose()


@pytest.mark.parametrize("autocommit", [True, False])
def test_unsupported_driver_transaction_mode_is_rejected(tmp_path, autocommit):
    engine = create_engine(
        URL.create("sqlite", database=str(tmp_path / "mode.db")),
        connect_args={"autocommit": autocommit},
    )
    try:
        with pytest.raises(ValueError, match="legacy transaction control"):
            reserve_next_job(engine, dispatcher_id=uuid4())
    finally:
        engine.dispose()


@pytest.mark.parametrize("timeout", [-1, 30001, True])
def test_invalid_timeout_cannot_make_lock_wait_unbounded(reservation_engine, timeout):
    with pytest.raises(ValueError, match="busy_timeout_ms"):
        reserve_next_job(
            reservation_engine, dispatcher_id=uuid4(), busy_timeout_ms=timeout
        )
