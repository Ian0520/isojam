import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput
from app.dispatcher import dispatch_once, reconcile_once
from app.execution import LocalFakeWorkerAdapter, WorkerInvocation
from app.fake_worker import run_fake_worker
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore
from tests.factories import create_test_upload, create_test_user


@pytest.fixture
def saved_result(reservation_engine, owned_reservation, tmp_path):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    invocation = WorkerInvocation.from_reservation(
        owned_reservation, uuid4()
    ).model_copy(update={"local_worker_id": uuid4()})
    store = LocalResultStore(tmp_path / "audio" / "results")
    assert (
        run_fake_worker(reservation_engine, invocation, result_store=store).status
        == "result_ready"
    )
    # Unit fixture simulates trusted termination evidence. The restart integration
    # below uses an actual child and the controller's OS-confirmed wait instead.
    assert reservations.record_execution_stopped(
        reservation_engine,
        owned_reservation,
        invocation_id=invocation.invocation_id,
        exit_code=0,
        local_worker_pid=os.getpid(),
        local_worker_id=invocation.local_worker_id,
    )
    return invocation, store


def state(engine):
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                select(model.__table__).order_by(*model.__table__.primary_key)
            ).all()
            for model in (Job, JobAttempt, JobOutput)
        )


def files(store, identity):
    bundle = store.verify_bundle(identity)
    paths = [output.path for output in bundle.outputs] + [
        store.root / bundle.manifest_key
    ]
    return [(path, path.stat().st_ino, path.read_bytes()) for path in paths]


def add_pending_job(engine):
    with Session(engine) as session:
        user = create_test_user(session, email=f"{uuid4().hex}@example.com")
        upload = create_test_upload(session, user)
        job = Job(upload_id=upload.id, status="pending", execution_backend="queued")
        session.add(job)
        session.commit()
        return job.id


def test_fresh_controller_recovers_real_worker_after_publication_commit_failure(
    reservation_engine,
):
    job_id = add_pending_job(reservation_engine)
    adapter = LocalFakeWorkerAdapter(reservation_engine)
    submit = Mock(wraps=adapter.submit)

    def fail_publication(connection):
        if (
            connection.scalar(
                select(JobAttempt.result_manifest_key).where(
                    JobAttempt.job_id == job_id
                )
            )
            is not None
        ):
            raise RuntimeError("publication interrupted")

    event.listen(reservation_engine, "commit", fail_publication)
    try:
        first = dispatch_once(
            reservation_engine,
            SimpleNamespace(submit=submit, results=adapter.results),
            dispatcher_id=uuid4(),
        )
    finally:
        event.remove(reservation_engine, "commit", fail_publication)
    assert first.status == "publication_unresolved"
    before = state(reservation_engine)
    attempt = before[1][0]
    assert attempt.phase == "running" and attempt.execution_stopped_at is not None
    assert before[0][0].status == "processing" and not before[2]
    identity = WorkerInvocation.from_reservation(
        reservations.JobReservation(
            attempt.job_id,
            attempt.id,
            attempt.attempt_number,
            attempt.dispatcher_id,
            attempt.dispatcher_generation,
            attempt.reservation_expires_at,
        ),
        attempt.invocation_id,
    ).result_identity()
    saved_files = files(adapter.results, identity)
    url = reservation_engine.url
    reservation_engine.dispose()
    fresh = create_engine(url)
    enable_sqlite_foreign_keys(fresh)
    no_launch = Mock(results=LocalResultStore(adapter.results.root))
    try:
        recovered = dispatch_once(fresh, no_launch, dispatcher_id=uuid4())
        assert recovered.status == "recovered" and recovered.job_id == job_id
        assert (
            recovered.attempt_id == first.attempt_id
            and recovered.invocation_id == first.invocation_id
        )
        assert (
            recovered.worker_pid == first.worker_pid
            and recovered.manifest_key == first.manifest_key
        )
        after = state(fresh)
        assert len(after[1]) == 1 and len(after[2]) == 7
        assert after[0][0].status == "completed" and after[1][0].phase == "succeeded"
        assert after[1][0].dispatcher_id == attempt.dispatcher_id
        assert after[1][0].dispatcher_generation == attempt.dispatcher_generation
        assert after[1][0].execution_stopped_at == attempt.execution_stopped_at
        assert files(no_launch.results, identity) == saved_files
        assert reconcile_once(fresh, no_launch.results) is None
        assert state(fresh) == after
        no_launch.submit.assert_not_called()
        submit.assert_called_once()
    finally:
        fresh.dispose()


def test_recovery_precedes_new_work_and_finishes_one_unit_per_cycle(
    reservation_engine, saved_result, clock
):
    invocation, store = saved_result
    next_job = add_pending_job(reservation_engine)
    adapter = Mock(results=store)
    recovered = dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    assert recovered.status == "recovered" and recovered.job_id == invocation.job_id
    with Session(reservation_engine) as session:
        assert session.get(Job, next_job).status == "pending"
        assert session.scalars(select(JobAttempt)).all()[0].job_id == invocation.job_id
        assert len(session.scalars(select(JobAttempt)).all()) == 1
    adapter.submit.assert_not_called()
    assert reconcile_once(reservation_engine, store) is None
    clock.now = datetime.now(UTC)
    assert (
        dispatch_once(
            reservation_engine,
            LocalFakeWorkerAdapter(reservation_engine),
            dispatcher_id=uuid4(),
        ).status
        == "completed"
    )
    with Session(reservation_engine) as session:
        assert session.get(Job, next_job).status == "completed"
        assert len(session.scalars(select(JobAttempt)).all()) == 2


@pytest.mark.parametrize(
    "change",
    [
        "missing_stop",
        "submitting",
        "failed_attempt",
        "failed_job",
        "local_backend",
        "newer_attempt",
        "unowned",
        "already_published",
    ],
)
def test_ineligible_or_unproven_attempt_is_not_inspected_or_executed(
    reservation_engine, saved_result, clock, change
):
    invocation, store = saved_result
    if change == "already_published":
        assert reconcile_once(reservation_engine, store).status == "recovered"
    else:
        with Session(reservation_engine) as session:
            attempt = session.get(JobAttempt, invocation.attempt_id)
            job = session.get(Job, invocation.job_id)
            if change in {"missing_stop", "submitting", "unowned"}:
                attempt.execution_stopped_at = None
                attempt.execution_exit_code = None
            if change in {"submitting", "unowned"}:
                attempt.local_worker_id = None
                attempt.local_worker_pid = None
                attempt.invocation_id = None
                attempt.started_at = None
                attempt.last_heartbeat_at = None
                attempt.phase = "submitting"
                job.status = "pending"
            if change == "unowned":
                attempt.dispatcher_id = None
                attempt.dispatcher_generation = 0
                attempt.reservation_expires_at = None
                attempt.execution_authorization_expires_at = None
            elif change == "failed_attempt":
                attempt.phase = "failed"
            elif change == "failed_job":
                job.status = "failed"
            elif change == "local_backend":
                job.execution_backend = "local"
            elif change == "newer_attempt":
                session.add(JobAttempt(job_id=job.id, attempt_number=2))
            session.commit()
    clock.now += timedelta(days=1)
    before = state(reservation_engine)
    verify = Mock(wraps=store.verify_bundle)
    store.verify_bundle = verify
    assert reservations.find_recoverable_attempt(reservation_engine) is None
    assert reconcile_once(reservation_engine, store) is None
    assert state(reservation_engine) == before
    verify.assert_not_called()


@pytest.mark.parametrize("damage", ["missing_manifest", "missing_stem", "changed_stem"])
def test_invalid_saved_bundle_preserves_attempt_and_blocks_new_delivery(
    reservation_engine, saved_result, damage
):
    invocation, store = saved_result
    bundle = store.verify_bundle(invocation.result_identity())
    if damage == "missing_manifest":
        (store.root / bundle.manifest_key).unlink()
    elif damage == "missing_stem":
        bundle.outputs[0].path.unlink()
    else:
        path = bundle.outputs[0].path
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"changed")
    add_pending_job(reservation_engine)
    before = state(reservation_engine)
    adapter = Mock(results=store)
    result = dispatch_once(reservation_engine, adapter, dispatcher_id=uuid4())
    assert (
        result.status == "result_invalid" and result.attempt_id == invocation.attempt_id
    )
    assert state(reservation_engine) == before
    adapter.submit.assert_not_called()


@pytest.mark.parametrize(
    "change", ["owner", "generation", "newer_attempt", "failed_job", "missing_stop"]
)
def test_recovery_releases_lookup_lock_and_rechecks_authority_after_verification(
    reservation_engine, saved_result, change
):
    invocation, store = saved_result
    verify = store.verify_bundle

    def change_after_verifying(*args, **kwargs):
        bundle = verify(*args, **kwargs)
        observed = create_engine(reservation_engine.url)
        try:
            with observed.connect() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout = 0")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.rollback()
            with Session(observed) as session:
                attempt = session.get(JobAttempt, invocation.attempt_id)
                if change == "owner":
                    attempt.dispatcher_id = uuid4()
                elif change == "generation":
                    attempt.dispatcher_generation += 1
                elif change == "newer_attempt":
                    session.add(JobAttempt(job_id=invocation.job_id, attempt_number=2))
                elif change == "failed_job":
                    session.get(Job, invocation.job_id).status = "failed"
                else:
                    attempt.execution_stopped_at = None
                    attempt.execution_exit_code = None
                session.commit()
        finally:
            observed.dispose()
        return bundle

    store.verify_bundle = change_after_verifying
    result = reconcile_once(reservation_engine, store)
    assert result.status == "publication_denied"
    with Session(reservation_engine) as session:
        assert not session.scalars(select(JobOutput)).all()
        assert session.get(JobAttempt, invocation.attempt_id).phase == "running"


def test_failed_recovery_commit_keeps_files_and_retry_changes_only_publication(
    reservation_engine, saved_result
):
    invocation, store = saved_result
    before = state(reservation_engine)
    saved_files = files(store, invocation.result_identity())

    def fail(connection):
        if (
            connection.scalar(
                select(JobAttempt.result_manifest_key).where(
                    JobAttempt.id == invocation.attempt_id
                )
            )
            is not None
        ):
            raise RuntimeError("recovery publication failed")

    event.listen(reservation_engine, "commit", fail)
    try:
        assert (
            reconcile_once(reservation_engine, store).status == "publication_unresolved"
        )
    finally:
        event.remove(reservation_engine, "commit", fail)
    assert state(reservation_engine) == before
    assert files(store, invocation.result_identity()) == saved_files
    assert reconcile_once(reservation_engine, store).status == "recovered"
    assert files(store, invocation.result_identity()) == saved_files


def test_conflicting_publication_is_reported_without_repair_or_new_work(
    reservation_engine, saved_result
):
    invocation, store = saved_result
    with Session(reservation_engine) as session:
        session.add(
            JobOutput(job_id=invocation.job_id, stem="vocals", path="/unexpected.wav")
        )
        session.commit()
    before = state(reservation_engine)
    assert reconcile_once(reservation_engine, store).status == "publication_conflict"
    assert state(reservation_engine) == before


def concurrent_recovery(database_url, root, barrier, reports):
    engine = create_engine(database_url)
    enable_sqlite_foreign_keys(engine)
    store = LocalResultStore(root)
    verify = store.verify_bundle

    def rendezvous(*args, **kwargs):
        bundle = verify(*args, **kwargs)
        barrier.wait(timeout=20)
        return bundle

    store.verify_bundle = rendezvous
    try:
        reports.put(asdict(reconcile_once(engine, store, busy_timeout_ms=5000)))
    except Exception as error:
        reports.put({"error": repr(error)})
    finally:
        engine.dispose()


def test_two_independent_controllers_acknowledge_one_unchanged_publication(
    reservation_engine, saved_result
):
    invocation, store = saved_result
    saved_files = files(store, invocation.result_identity())
    context = multiprocessing.get_context("spawn")
    barrier, reports = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=concurrent_recovery,
            args=(str(reservation_engine.url), store.root, barrier, reports),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        results = [reports.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=20)
            assert process.exitcode == 0
        assert all(result.get("status") == "recovered" for result in results), results
        assert all(result["attempt_id"] == invocation.attempt_id for result in results)
        after = state(reservation_engine)
        assert len(after[1]) == 1 and len(after[2]) == 7
        assert after[0][0].status == "completed" and after[1][0].phase == "succeeded"
        assert files(store, invocation.result_identity()) == saved_files
        assert reconcile_once(reservation_engine, store) is None
        assert state(reservation_engine) == after
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        reports.close()
        reports.join_thread()


def test_recovery_only_cli_recovers_once_and_never_dispatches_pending_work(
    reservation_engine, saved_result
):
    invocation, _ = saved_result
    next_job = add_pending_job(reservation_engine)
    for expected in ("recovered", "idle"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "app.dispatcher",
                "--once",
                "--adapter",
                "local-fake",
                "--reconcile-only",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["status"] == expected
        if expected == "recovered":
            assert report["job_id"] == str(invocation.job_id)
        with Session(reservation_engine) as session:
            assert session.get(Job, next_job).status == "pending"
            assert len(session.scalars(select(JobAttempt)).all()) == 1


def test_lookup_contention_and_recovery_only_cli_preserve_state_with_bounded_wait(
    reservation_engine, saved_result
):
    _, store = saved_result
    before = state(reservation_engine)
    observed = create_engine(reservation_engine.url)
    try:
        with observed.connect() as writer:
            writer.exec_driver_sql("BEGIN EXCLUSIVE")
            with pytest.raises(reservations.ReservationBusyError):
                reconcile_once(reservation_engine, store, busy_timeout_ms=0)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.dispatcher",
                    "--once",
                    "--adapter",
                    "local-fake",
                    "--reconcile-only",
                    "--busy-timeout-ms",
                    "0",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 75
            assert json.loads(result.stdout) == {"status": "database_busy"}
            writer.rollback()
        assert state(reservation_engine) == before
        with reservation_engine.connect() as connection:
            assert not connection.connection.dbapi_connection.in_transaction
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            )
        assert reconcile_once(reservation_engine, store).status == "recovered"
    finally:
        observed.dispose()


def test_reader_blocked_recovery_commit_rolls_back_outputs_and_allows_later_publication(
    reservation_engine, saved_result
):
    _, store = saved_result
    before = state(reservation_engine)
    verify = Mock(wraps=store.verify_bundle)
    store.verify_bundle = verify
    observed = create_engine(reservation_engine.url)
    try:
        with observed.connect() as reader:
            reader.exec_driver_sql("BEGIN")
            reader.exec_driver_sql("SELECT * FROM jobs").all()
            with pytest.raises(reservations.ReservationBusyError):
                reconcile_once(reservation_engine, store, busy_timeout_ms=0)
            verify.assert_called_once()
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM job_outputs").scalar_one()
                == 0
            )
            reader.rollback()
        assert state(reservation_engine) == before
        assert reconcile_once(reservation_engine, store).status == "recovered"
    finally:
        observed.dispose()
