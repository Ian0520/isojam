import json
import multiprocessing
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import enable_sqlite_foreign_keys
from app.db_models import Job, JobAttempt, JobOutput
from app.execution import WorkerInvocation
from app.fake_worker import create_fake_workspace, run_fake_worker
from app.publication import publish_result
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore, ResultValidationError


@pytest.fixture
def ready_result(reservation_engine, owned_reservation, tmp_path):
    assert reservations.begin_submission(reservation_engine, owned_reservation)
    invocation = WorkerInvocation.from_reservation(owned_reservation, uuid4())
    store = LocalResultStore(tmp_path / "audio" / "results")
    assert (
        run_fake_worker(reservation_engine, invocation, result_store=store).status
        == "result_ready"
    )
    return invocation, store


def snapshot(engine, reservation):
    with Session(engine) as session:
        attempt = session.get(JobAttempt, reservation.attempt_id)
        job = session.get(Job, reservation.job_id)
        outputs = session.scalars(
            select(JobOutput).where(JobOutput.job_id == job.id).order_by(JobOutput.stem)
        ).all()
        return (
            {
                column.name: getattr(attempt, column.name)
                for column in JobAttempt.__table__.columns
            },
            {
                column.name: getattr(job, column.name)
                for column in Job.__table__.columns
            },
            [(row.stem, row.path) for row in outputs],
        )


def publish(engine, invocation, store, **kwargs):
    return publish_result(
        engine,
        invocation.reservation(),
        invocation_id=invocation.invocation_id,
        store=store,
        **kwargs,
    )


def test_publication_commits_complete_output_set_and_identical_replay_changes_nothing(
    reservation_engine, owned_reservation, ready_result, clock
):
    invocation, store = ready_result
    bundle = store.verify_bundle(invocation.result_identity())
    files = [
        (output.path.stat().st_ino, output.path.read_bytes())
        for output in bundle.outputs
    ]
    assert publish(reservation_engine, invocation, store)
    attempt, job, outputs = snapshot(reservation_engine, owned_reservation)
    assert job["status"] == "completed" and attempt["phase"] == "succeeded"
    assert (
        attempt["finished_at"] >= attempt["last_heartbeat_at"] >= attempt["started_at"]
    )
    assert attempt["result_manifest_key"] == bundle.manifest_key
    assert attempt["result_manifest_sha256"] == bundle.manifest_sha256
    assert dict(outputs) == {output.stem: str(output.path) for output in bundle.outputs}
    before = snapshot(reservation_engine, owned_reservation)
    clock.now += timedelta(days=1)
    assert publish(reservation_engine, invocation, store)
    assert snapshot(reservation_engine, owned_reservation) == before
    assert [
        (output.path.stat().st_ino, output.path.read_bytes())
        for output in bundle.outputs
    ] == files
    assert not reservations.authorize_execution(
        reservation_engine, owned_reservation, invocation_id=invocation.invocation_id
    )
    assert not reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=invocation.invocation_id
    )


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
def test_valid_files_cannot_publish_with_wrong_execution_authority(
    reservation_engine, owned_reservation, ready_result, tmp_path, field
):
    invocation, store = ready_result
    changes = {
        field: 2 if field in {"attempt_number", "dispatcher_generation"} else uuid4()
    }
    wrong = invocation.model_copy(update=changes)
    # Even a complete valid bundle under the claimed identity cannot grant authority.
    workspace = tmp_path / "other-workspace"
    workspace.mkdir()
    create_fake_workspace(workspace)
    store.write_bundle(wrong.result_identity(), workspace)
    before = snapshot(reservation_engine, owned_reservation)
    assert not publish(reservation_engine, wrong, store)
    assert snapshot(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize(
    "change",
    ["newer_attempt", "local_backend", "job_failed", "attempt_failed", "unowned"],
)
def test_stale_or_ineligible_database_state_rejects_publication(
    reservation_engine, owned_reservation, ready_result, change
):
    invocation, store = ready_result
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        job = session.get(Job, owned_reservation.job_id)
        if change == "newer_attempt":
            session.add(JobAttempt(job_id=job.id, attempt_number=2))
        elif change == "local_backend":
            job.execution_backend = "local"
        elif change == "job_failed":
            job.status = "failed"
        elif change == "attempt_failed":
            attempt.phase = "failed"
        else:
            attempt.dispatcher_id = None
            attempt.dispatcher_generation = 0
            attempt.reservation_expires_at = None
            attempt.execution_authorization_expires_at = None
            attempt.invocation_id = None
            attempt.started_at = None
            attempt.last_heartbeat_at = None
        session.commit()
    before = snapshot(reservation_engine, owned_reservation)
    assert not publish(reservation_engine, invocation, store)
    assert snapshot(reservation_engine, owned_reservation) == before


def test_authority_is_rechecked_after_file_verification_without_holding_write_lock(
    reservation_engine, owned_reservation, ready_result, monkeypatch
):
    invocation, store = ready_result
    verify = store.verify_bundle

    def change_owner_during_verification(*args, **kwargs):
        observed = create_engine(reservation_engine.url)
        try:
            with observed.begin() as connection:
                connection.exec_driver_sql("PRAGMA busy_timeout = 25")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.exec_driver_sql(
                    "UPDATE job_attempts SET dispatcher_generation = dispatcher_generation + 1 WHERE id = ?",
                    (owned_reservation.attempt_id.hex,),
                )
        finally:
            observed.dispose()
        return verify(*args, **kwargs)

    monkeypatch.setattr(store, "verify_bundle", change_owner_during_verification)
    assert not publish(reservation_engine, invocation, store)
    attempt, job, outputs = snapshot(reservation_engine, owned_reservation)
    assert attempt["dispatcher_generation"] == 2
    assert (
        attempt["phase"] == "running"
        and job["status"] == "processing"
        and outputs == []
    )


@pytest.mark.parametrize("damage", ["missing_file", "hash", "profile"])
def test_invalid_result_never_enters_publication_transaction(
    reservation_engine, owned_reservation, ready_result, damage
):
    invocation, store = ready_result
    bundle = store.verify_bundle(invocation.result_identity())
    if damage == "missing_file":
        bundle.outputs[0].path.unlink()
    else:
        path = store.root / bundle.manifest_key
        body = json.loads(path.read_bytes())
        if damage == "hash":
            body["outputs"][0]["sha256"] = "0" * 64
        else:
            body["profile_id"] = "real-inference"
        path.chmod(0o600)
        path.write_text(json.dumps(body))
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    before = snapshot(reservation_engine, owned_reservation)
    event.listen(reservation_engine, "before_cursor_execute", record)
    try:
        with pytest.raises(ResultValidationError):
            publish(reservation_engine, invocation, store)
    finally:
        event.remove(reservation_engine, "before_cursor_execute", record)
    assert statements == []
    assert snapshot(reservation_engine, owned_reservation) == before


def test_different_valid_manifest_cannot_replace_existing_publication(
    reservation_engine, owned_reservation, ready_result
):
    invocation, store = ready_result
    assert publish(reservation_engine, invocation, store)
    before = snapshot(reservation_engine, owned_reservation)
    path = store.root / store.manifest_key(invocation.result_identity())
    path.chmod(0o600)
    path.write_bytes(path.read_bytes() + b"\n")
    # The same semantic JSON still verifies, but its finalized bytes differ.
    store.verify_bundle(invocation.result_identity())
    with pytest.raises(reservations.PublicationConflictError):
        publish(reservation_engine, invocation, store)
    assert snapshot(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize(
    "existing",
    ["partial_before_publish", "missing_after_publish", "changed_path_after_publish"],
)
def test_existing_output_records_are_not_overwritten_or_silently_repaired(
    reservation_engine, owned_reservation, ready_result, existing
):
    invocation, store = ready_result
    if existing != "partial_before_publish":
        assert publish(reservation_engine, invocation, store)
    with Session(reservation_engine) as session:
        if existing == "partial_before_publish":
            session.add(
                JobOutput(
                    job_id=owned_reservation.job_id, stem="vocals", path="/existing.wav"
                )
            )
        else:
            output = session.get(JobOutput, (owned_reservation.job_id, "vocals"))
            if existing == "missing_after_publish":
                session.delete(output)
            else:
                output.path = "/different.wav"
        session.commit()
    before = snapshot(reservation_engine, owned_reservation)
    with pytest.raises(reservations.PublicationConflictError):
        publish(reservation_engine, invocation, store)
    assert snapshot(reservation_engine, owned_reservation) == before


@pytest.mark.parametrize("failure", ["after_outputs", "commit"])
def test_failed_publication_rolls_back_every_database_change_and_files_allow_retry(
    reservation_engine, owned_reservation, ready_result, failure
):
    invocation, store = ready_result
    before = snapshot(reservation_engine, owned_reservation)
    bundle = store.verify_bundle(invocation.result_identity())

    def fail_commit(connection):
        raise RuntimeError("publication interrupted")

    def fail_after_outputs(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("INSERT INTO job_outputs"):
            raise RuntimeError("publication interrupted")

    name, callback = (
        ("commit", fail_commit)
        if failure == "commit"
        else ("after_cursor_execute", fail_after_outputs)
    )
    event.listen(reservation_engine, name, callback)
    try:
        with pytest.raises(RuntimeError, match="interrupted"):
            publish(reservation_engine, invocation, store)
    finally:
        event.remove(reservation_engine, name, callback)
    assert snapshot(reservation_engine, owned_reservation) == before
    assert store.verify_bundle(invocation.result_identity()) == bundle
    assert publish(reservation_engine, invocation, store)
    assert len(snapshot(reservation_engine, owned_reservation)[2]) == 7


def test_real_commit_contention_cleans_up_driver_transaction_and_retry_succeeds(
    reservation_engine, owned_reservation, ready_result
):
    invocation, store = ready_result
    before = snapshot(reservation_engine, owned_reservation)
    observed = create_engine(reservation_engine.url)
    try:
        with observed.connect() as reader:
            reader.exec_driver_sql("BEGIN")
            reader.exec_driver_sql("SELECT * FROM jobs").all()
            with pytest.raises(reservations.ReservationBusyError):
                publish(reservation_engine, invocation, store, busy_timeout_ms=0)
            # A fresh reader still sees no partial publication while holding its lock.
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM job_outputs").scalar_one()
                == 0
            )
            reader.rollback()
        assert snapshot(reservation_engine, owned_reservation) == before
        with reservation_engine.connect() as connection:
            assert not connection.connection.dbapi_connection.in_transaction
            assert (
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            )
        assert publish(reservation_engine, invocation, store)
    finally:
        observed.dispose()


def test_completion_timestamp_does_not_regress_after_clock_moves_backwards(
    reservation_engine, owned_reservation, ready_result, clock
):
    invocation, store = ready_result
    clock.now += timedelta(seconds=30)
    assert reservations.record_heartbeat(
        reservation_engine, owned_reservation, invocation_id=invocation.invocation_id
    )
    heartbeat = clock.now
    clock.now -= timedelta(seconds=60)
    assert publish(reservation_engine, invocation, store)
    assert (
        snapshot(reservation_engine, owned_reservation)[0]["finished_at"] == heartbeat
    )


def test_publication_after_start_deadline_can_release_slot_for_next_pending_job(
    reservation_engine, owned_reservation, ready_result, clock
):
    invocation, store = ready_result
    clock.now += timedelta(days=1)
    with Session(reservation_engine) as session:
        upload_id = session.get(Job, owned_reservation.job_id).upload_id
        next_job = Job(
            upload_id=upload_id, execution_backend="queued", status="pending"
        )
        session.add(next_job)
        session.commit()
        next_id = next_job.id
    assert (
        reservations.reserve_next_job(reservation_engine, dispatcher_id=uuid4()) is None
    )
    assert publish(reservation_engine, invocation, store)
    next_reservation = reservations.reserve_next_job(
        reservation_engine, dispatcher_id=uuid4()
    )
    assert next_reservation.job_id == next_id


def competing_publisher(url, token, invocation_id, root, barrier, outcomes):
    engine = create_engine(url)
    enable_sqlite_foreign_keys(engine)
    store = LocalResultStore(Path(root))
    verify = store.verify_bundle

    def meet(*args, **kwargs):
        bundle = verify(*args, **kwargs)
        barrier.wait(timeout=15)
        return bundle

    store.verify_bundle = meet
    try:
        outcomes.put(
            publish_result(
                engine,
                token,
                invocation_id=invocation_id,
                store=store,
                busy_timeout_ms=5000,
            )
        )
    except Exception as error:
        outcomes.put(repr(error))
    finally:
        engine.dispose()


def test_independent_publishers_acknowledge_one_publication_without_duplicate_outputs(
    reservation_engine, owned_reservation, ready_result
):
    invocation, store = ready_result
    context = multiprocessing.get_context("spawn")
    barrier, outcomes = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=competing_publisher,
            args=(
                str(reservation_engine.url),
                owned_reservation,
                invocation.invocation_id,
                str(store.root),
                barrier,
                outcomes,
            ),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert [outcomes.get(timeout=25) for _ in processes] == [True, True]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        with Session(reservation_engine) as session:
            assert session.scalar(select(func.count()).select_from(JobOutput)) == 7
        assert (
            snapshot(reservation_engine, owned_reservation)[0]["phase"] == "succeeded"
        )
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        outcomes.close()
        outcomes.join_thread()


@pytest.mark.parametrize(
    "damage",
    [
        "key_only",
        "hash_only",
        "short_hash",
        "nonhex",
        "uppercase",
        "long_key",
        "unfinished",
        "clock_order",
    ],
)
def test_schema_rejects_incomplete_or_invalid_publication_metadata(
    reservation_engine, owned_reservation, ready_result, clock, damage
):
    invocation, store = ready_result
    bundle = store.verify_bundle(invocation.result_identity())
    with Session(reservation_engine) as session:
        attempt = session.get(JobAttempt, owned_reservation.attempt_id)
        attempt.phase = "succeeded"
        attempt.finished_at = clock.now
        attempt.result_manifest_key = bundle.manifest_key
        attempt.result_manifest_sha256 = bundle.manifest_sha256
        if damage == "key_only":
            attempt.result_manifest_sha256 = None
        elif damage == "hash_only":
            attempt.result_manifest_key = None
        elif damage == "short_hash":
            attempt.result_manifest_sha256 = "a" * 63
        elif damage == "nonhex":
            attempt.result_manifest_sha256 = "z" * 64
        elif damage == "uppercase":
            attempt.result_manifest_sha256 = "A" * 64
        elif damage == "long_key":
            attempt.result_manifest_key = "x" * 257
        elif damage == "unfinished":
            attempt.phase = "running"
            attempt.finished_at = None
        else:
            attempt.finished_at = attempt.started_at - timedelta(seconds=1)
        with pytest.raises(IntegrityError, match="ck_job_attempts_result_publication"):
            session.commit()


def test_readers_cannot_see_output_rows_before_the_completion_transaction_commits(
    reservation_engine, ready_result
):
    invocation, store = ready_result
    observed = create_engine(reservation_engine.url)
    inspections = []

    def inspect_after_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("INSERT INTO job_outputs"):
            with observed.connect() as reader:
                status = reader.exec_driver_sql("SELECT status FROM jobs").scalar_one()
                count = reader.exec_driver_sql(
                    "SELECT count(*) FROM job_outputs"
                ).scalar_one()
                inspections.append((status, count))

    event.listen(reservation_engine, "after_cursor_execute", inspect_after_insert)
    try:
        assert publish(reservation_engine, invocation, store)
        assert inspections == [("processing", 0)]
        with observed.connect() as reader:
            assert (
                reader.exec_driver_sql("SELECT status FROM jobs").scalar_one()
                == "completed"
            )
            assert (
                reader.exec_driver_sql("SELECT count(*) FROM job_outputs").scalar_one()
                == 7
            )
    finally:
        event.remove(reservation_engine, "after_cursor_execute", inspect_after_insert)
        observed.dispose()
