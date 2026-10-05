import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, and_, func, insert, or_, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.elements import ColumnElement

from app.db_models import Job, JobAttempt, JobOutput
from app.results import ResultIdentity, VerifiedBundle

TERMINAL_ATTEMPT_PHASES = frozenset({"succeeded", "failed"})


class ReservationBusyError(RuntimeError):
    """SQLite contention prevented a reservation operation; the caller may try again later."""


@dataclass(frozen=True)
class JobReservation:
    job_id: UUID
    attempt_id: UUID
    attempt_number: int
    dispatcher_id: UUID
    dispatcher_generation: int
    reservation_expires_at: datetime


@contextmanager
def _reservation_transaction(
    engine: Engine, busy_timeout_ms: int
) -> Iterator[Connection]:
    if engine.dialect.name != "sqlite":
        raise ValueError("Job reservation currently supports SQLite only")
    if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 30000:
        raise ValueError("busy_timeout_ms must be an integer between 0 and 30000")
    with engine.connect() as connection:
        driver = connection.connection.dbapi_connection
        # The application's pinned Python/SQLite engine uses legacy transaction
        # control. Modern autocommit modes have different BEGIN/commit behavior.
        if driver.autocommit != sqlite3.LEGACY_TRANSACTION_CONTROL:
            raise ValueError("Reservation requires SQLite legacy transaction control")
        if driver.in_transaction:
            raise RuntimeError("Reservation requires a fresh database transaction")
        previous_timeout = connection.exec_driver_sql(
            "PRAGMA busy_timeout"
        ).scalar_one()
        connection.rollback()
        try:
            # PRAGMA does not support bound parameters; the caller validated int.
            connection.exec_driver_sql(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except OperationalError as error:
            code = getattr(error.orig, "sqlite_errorcode", None)
            if code is not None and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }:
                raise ReservationBusyError(
                    "Database is busy; try reserving work again later"
                ) from error
            raise
        finally:
            # SQLite can retain a transaction after SQLITE_BUSY at COMMIT,
            # even though SQLAlchemy has marked its transaction inactive.
            # Roll back the driver first, then clear SQLAlchemy's transaction.
            driver.rollback()
            connection.rollback()
            connection.exec_driver_sql(f"PRAGMA busy_timeout = {previous_timeout}")
            connection.rollback()


def reserve_next_job(
    engine: Engine,
    *,
    dispatcher_id: UUID,
    reservation_ttl_seconds: int = 60,
    busy_timeout_ms: int = 1000,
) -> JobReservation | None:
    """Commit a first-attempt reservation for the oldest eligible queued job.

    A non-terminal queued attempt occupies the single global slot regardless of
    job status or heartbeat age. Any previous attempt excludes a job here: retries
    need separate authorization/recovery rules. Local jobs stay outside this queue.
    No model loading, worker launch, or network call belongs in this transaction.

    SQLite lock waits are bounded per operation (0-30000 ms, default 1000).
    Contention raises ReservationBusyError; no work/occupied capacity returns None.
    This operation owns a fresh connection and its commit, unlike repositories
    participating in an existing request session. The caller may use the returned
    identifiers only after the commit succeeds. Use a fresh dispatcher UUID per
    process lifetime. Reservation lifetime is 1-3600 seconds (default 60); it is
    pre-submission ownership, not a worker execution deadline.
    """
    _validate_dispatcher_id(dispatcher_id)
    _validate_reservation_lifetime(reservation_ttl_seconds)

    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        active_attempt = connection.scalar(
            select(JobAttempt.id)
            .join(Job, Job.id == JobAttempt.job_id)
            .where(
                Job.execution_backend == "queued",
                JobAttempt.phase.not_in(TERMINAL_ATTEMPT_PHASES),
            )
            .limit(1)
        )
        if active_attempt is not None:
            return None

        has_attempt = select(JobAttempt.id).where(JobAttempt.job_id == Job.id).exists()
        job_id = connection.scalar(
            select(Job.id)
            .where(
                Job.execution_backend == "queued",
                Job.status == "pending",
                ~has_attempt,
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        if job_id is None:
            return None

        attempt_id = uuid4()
        expires_at = _utc_now() + timedelta(seconds=reservation_ttl_seconds)
        connection.execute(
            insert(JobAttempt).values(
                id=attempt_id,
                job_id=job_id,
                attempt_number=1,
                phase="reserved",
                dispatcher_id=dispatcher_id,
                dispatcher_generation=1,
                reservation_expires_at=expires_at,
            )
        )
        return JobReservation(job_id, attempt_id, 1, dispatcher_id, 1, expires_at)


def _utc_now() -> datetime:
    # Control processes share the SQLite host clock. Sample only after obtaining
    # the write lock; time spent waiting for the lock must not extend authority.
    return datetime.now(UTC)


def _validate_dispatcher_id(dispatcher_id: UUID) -> None:
    if not isinstance(dispatcher_id, UUID):
        raise ValueError("dispatcher_id must be a UUID")


def _validate_reservation_lifetime(reservation_ttl_seconds: int) -> None:
    if (
        type(reservation_ttl_seconds) is not int
        or not 1 <= reservation_ttl_seconds <= 3600
    ):
        raise ValueError(
            "reservation_ttl_seconds must be an integer between 1 and 3600"
        )


def _current_queued_attempt(job_status: str) -> ColumnElement[bool]:
    eligible_job = (
        select(Job.id)
        .where(
            Job.id == JobAttempt.job_id,
            Job.execution_backend == "queued",
            Job.status == job_status,
        )
        .exists()
    )
    newer = JobAttempt.__table__.alias("newer_attempt")
    has_newer_attempt = (
        select(newer.c.id)
        .where(
            newer.c.job_id == JobAttempt.job_id,
            newer.c.attempt_number > JobAttempt.attempt_number,
        )
        .exists()
    )
    return eligible_job & ~has_newer_attempt


def reclaim_reservation(
    engine: Engine,
    attempt_id: UUID,
    *,
    dispatcher_id: UUID,
    reservation_ttl_seconds: int = 60,
    busy_timeout_ms: int = 1000,
) -> JobReservation | None:
    """Commit new ownership of an expired, owned, pre-submission reservation.

    The attempt and its occupied slot are preserved; this is not an inference
    retry. Generation increases even when the same dispatcher reclaims. Existing
    unowned attempts are not granted authority automatically. Submission or any
    later phase forbids takeover, regardless of expiry or heartbeat age.
    """
    _validate_dispatcher_id(dispatcher_id)
    _validate_reservation_lifetime(reservation_ttl_seconds)
    if not isinstance(attempt_id, UUID):
        raise ValueError("attempt_id must be a UUID")
    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        now = _utc_now()
        row = connection.execute(
            update(JobAttempt)
            .where(
                JobAttempt.id == attempt_id,
                JobAttempt.phase == "reserved",
                JobAttempt.dispatcher_id.is_not(None),
                JobAttempt.dispatcher_generation > 0,
                JobAttempt.reservation_expires_at <= now,
                _current_queued_attempt("pending"),
            )
            .values(
                dispatcher_id=dispatcher_id,
                dispatcher_generation=JobAttempt.dispatcher_generation + 1,
                reservation_expires_at=now + timedelta(seconds=reservation_ttl_seconds),
                updated_at=now,
            )
            .returning(
                JobAttempt.job_id,
                JobAttempt.id,
                JobAttempt.attempt_number,
                JobAttempt.dispatcher_id,
                JobAttempt.dispatcher_generation,
                JobAttempt.reservation_expires_at,
            )
        ).one_or_none()
        return None if row is None else JobReservation(*row)


def begin_submission(
    engine: Engine,
    reservation: JobReservation,
    *,
    authorization_ttl_seconds: int = 300,
    busy_timeout_ms: int = 1000,
) -> bool:
    """Commit submission intent once for a current, unexpired reservation.

    True is returned only after the transition commits. Only that successful
    caller may make the initial external submission. A duplicate/stale call
    returns False; a commit error raises. Never contact a worker inside the
    transaction. A crash or ambiguous response after intent requires reconciliation,
    not another call to submit. The 1-3600 second authorization lifetime (default
    300) limits when an invocation may receive permission, not its running time.
    Repeated calls never extend that deadline. This is not worker execution permission.
    """
    _validate_reservation(reservation)
    _validate_authorization_lifetime(authorization_ttl_seconds)
    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        now = _utc_now()
        changed = connection.scalar(
            update(JobAttempt)
            .where(
                JobAttempt.id == reservation.attempt_id,
                JobAttempt.job_id == reservation.job_id,
                JobAttempt.attempt_number == reservation.attempt_number,
                JobAttempt.dispatcher_id == reservation.dispatcher_id,
                JobAttempt.dispatcher_generation == reservation.dispatcher_generation,
                JobAttempt.phase == "reserved",
                JobAttempt.reservation_expires_at > now,
                _current_queued_attempt("pending"),
            )
            .values(
                phase="submitting",
                execution_authorization_expires_at=now
                + timedelta(seconds=authorization_ttl_seconds),
                updated_at=now,
            )
            .returning(JobAttempt.id)
        )
        return changed is not None


def _validate_reservation(reservation: JobReservation) -> None:
    _validate_dispatcher_id(reservation.dispatcher_id)
    if not isinstance(reservation.job_id, UUID) or not isinstance(
        reservation.attempt_id, UUID
    ):
        raise ValueError("reservation identifiers must be UUIDs")
    for value in (reservation.attempt_number, reservation.dispatcher_generation):
        if type(value) is not int or value < 1:
            raise ValueError(
                "reservation number and generation must be positive integers"
            )


def _validate_authorization_lifetime(authorization_ttl_seconds: int) -> None:
    if (
        type(authorization_ttl_seconds) is not int
        or not 1 <= authorization_ttl_seconds <= 3600
    ):
        raise ValueError(
            "authorization_ttl_seconds must be an integer between 1 and 3600"
        )


def authorize_execution(
    engine: Engine,
    reservation: JobReservation,
    *,
    invocation_id: UUID,
    busy_timeout_ms: int = 1000,
) -> bool:
    """Commit permission for one fresh invocation to execute the current attempt.

    Only the first successful caller receives True, after attempt and public job
    state commit together. Duplicate requests, including the same invocation ID,
    return False and never grant another execution. A fresh invocation UUID belongs
    to one worker execution, not to a provider request or a reusable worker process.

    This trusted internal operation is not an authenticated remote control API.
    Workers must execute only after receiving True and must not infer if permission
    is denied, commit fails, or its acknowledgement is lost. An already persisted
    invocation holds capacity even after its authorization deadline; expiry is
    neither cancellation nor proof of worker termination. No inference belongs
    inside this short transaction.
    """
    _validate_reservation(reservation)
    _validate_invocation_id(invocation_id)
    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        now = _utc_now()
        # Refuse reusing an invocation on another attempt. The unique constraint
        # also protects this invariant from other database writers.
        if (
            connection.scalar(
                select(JobAttempt.id).where(JobAttempt.invocation_id == invocation_id)
            )
            is not None
        ):
            return False
        changed = connection.scalar(
            update(JobAttempt)
            .where(
                JobAttempt.id == reservation.attempt_id,
                JobAttempt.job_id == reservation.job_id,
                JobAttempt.attempt_number == reservation.attempt_number,
                JobAttempt.dispatcher_id == reservation.dispatcher_id,
                JobAttempt.dispatcher_generation == reservation.dispatcher_generation,
                JobAttempt.phase.in_(("submitting", "submitted", "uncertain")),
                JobAttempt.invocation_id.is_(None),
                JobAttempt.started_at.is_(None),
                JobAttempt.finished_at.is_(None),
                JobAttempt.execution_authorization_expires_at > now,
                _current_queued_attempt("pending"),
            )
            .values(
                phase="running",
                invocation_id=invocation_id,
                started_at=now,
                updated_at=now,
            )
            .returning(JobAttempt.id)
        )
        if changed is None:
            return False
        job_changed = connection.scalar(
            update(Job)
            .where(
                Job.id == reservation.job_id,
                Job.execution_backend == "queued",
                Job.status == "pending",
            )
            .values(status="processing", updated_at=now)
            .returning(Job.id)
        )
        if job_changed is None:
            raise RuntimeError(
                "Execution authorization could not update its pending job"
            )
        return True


def _validate_invocation_id(invocation_id: UUID) -> None:
    if not isinstance(invocation_id, UUID):
        raise ValueError("invocation_id must be a UUID")


def record_heartbeat(
    engine: Engine,
    reservation: JobReservation,
    *,
    invocation_id: UUID,
    busy_timeout_ms: int = 1000,
) -> bool:
    """Commit contact evidence from the current authorized running invocation.

    True acknowledges a valid report, including repeated reports; it does not
    grant execution permission. Identity/generation, latest attempt, phase and
    public job state are checked atomically. Stale or ineligible reports return
    False; a failed commit raises. No timestamp is accepted from the worker.

    Control-side UTC time is sampled after acquiring the write lock. Stored
    heartbeat/update times never regress if the clock moves backwards. The first
    heartbeat is not invented at authorization or migration. Once authorized, an
    invocation may report after its authorization deadline; heartbeats do not
    renew deadlines, change ownership, or free the slot. Missing reports are
    suspicion, not proof of termination. This is a trusted internal repository
    operation; the sending loop and authenticated remote wrapper follow later.
    """
    _validate_reservation(reservation)
    _validate_invocation_id(invocation_id)
    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        now = _utc_now()
        changed = connection.scalar(
            update(JobAttempt)
            .where(
                JobAttempt.id == reservation.attempt_id,
                JobAttempt.job_id == reservation.job_id,
                JobAttempt.attempt_number == reservation.attempt_number,
                JobAttempt.dispatcher_id == reservation.dispatcher_id,
                JobAttempt.dispatcher_generation == reservation.dispatcher_generation,
                JobAttempt.invocation_id == invocation_id,
                JobAttempt.started_at.is_not(None),
                JobAttempt.finished_at.is_(None),
                JobAttempt.phase == "running",
                _current_queued_attempt("processing"),
            )
            .values(
                # SQLite's multi-argument max is scalar. Coalesce avoids NULL
                # suppressing the first report; start time bounds clock rollback.
                last_heartbeat_at=func.max(
                    now,
                    JobAttempt.started_at,
                    func.coalesce(JobAttempt.last_heartbeat_at, now),
                ),
                updated_at=func.max(now, JobAttempt.updated_at),
            )
            .returning(JobAttempt.id)
        )
        return changed is not None


class PublicationConflictError(ValueError):
    """Existing database publication cannot be replaced or silently repaired."""


def publish_verified_result(
    engine: Engine,
    reservation: JobReservation,
    *,
    invocation_id: UUID,
    bundle: VerifiedBundle,
    busy_timeout_ms: int = 1000,
) -> bool:
    """Commit output rows, selected manifest and completion as one unit.

    Trusted callers must verify storage and establish that execution has ended
    before entering this operation. No file read or worker call belongs inside
    its transaction. Identity/owner/generation/latest attempt are rechecked here.
    Deadline expiry does not undo an execution already authorized to run.

    True acknowledges initial or identical existing publication after commit.
    Stale/ineligible authority returns False. Conflicting existing state raises;
    failed commits leave durable files for a separate publication retry, never
    permission for another inference. Existing output records are not overwritten.
    """
    _validate_reservation(reservation)
    _validate_invocation_id(invocation_id)
    identity = ResultIdentity(
        job_id=reservation.job_id,
        attempt_id=reservation.attempt_id,
        invocation_id=invocation_id,
    )
    if bundle.manifest.identity != identity:
        raise ValueError("Verified bundle does not match publication identity")
    proposed_outputs = {
        artifact.stem: str(artifact.path) for artifact in bundle.outputs
    }
    if not proposed_outputs or len(proposed_outputs) != len(bundle.outputs):
        raise ValueError("Verified bundle must contain unique outputs")

    with _reservation_transaction(engine, busy_timeout_ms) as connection:
        attempt = (
            connection.execute(
                select(JobAttempt.__table__).where(
                    JobAttempt.id == reservation.attempt_id,
                    JobAttempt.job_id == reservation.job_id,
                    JobAttempt.attempt_number == reservation.attempt_number,
                    JobAttempt.dispatcher_id == reservation.dispatcher_id,
                    JobAttempt.dispatcher_generation
                    == reservation.dispatcher_generation,
                    JobAttempt.invocation_id == invocation_id,
                    JobAttempt.started_at.is_not(None),
                    or_(
                        and_(
                            JobAttempt.phase == "running",
                            JobAttempt.finished_at.is_(None),
                            _current_queued_attempt("processing"),
                        ),
                        and_(
                            JobAttempt.phase == "succeeded",
                            JobAttempt.finished_at.is_not(None),
                            _current_queued_attempt("completed"),
                        ),
                    ),
                )
            )
            .mappings()
            .one_or_none()
        )
        if attempt is None:
            return False
        existing_outputs = dict(
            connection.execute(
                select(JobOutput.stem, JobOutput.path).where(
                    JobOutput.job_id == reservation.job_id
                )
            ).all()
        )
        if attempt["phase"] == "succeeded":
            if (
                attempt["result_manifest_key"] != bundle.manifest_key
                or attempt["result_manifest_sha256"] != bundle.manifest_sha256
                or existing_outputs != proposed_outputs
            ):
                raise PublicationConflictError(
                    "Result differs from existing publication"
                )
            # Repeated acknowledgement does not rewrite rows or timestamps.
            return True
        if (
            existing_outputs
            or attempt["result_manifest_key"] is not None
            or attempt["result_manifest_sha256"] is not None
        ):
            raise PublicationConflictError(
                "Running job already has publication records"
            )
        now = _utc_now()
        finished = max(
            now,
            attempt["started_at"],
            attempt["last_heartbeat_at"] or attempt["started_at"],
        )
        connection.execute(
            insert(JobOutput),
            [
                {"job_id": reservation.job_id, "stem": stem, "path": path}
                for stem, path in proposed_outputs.items()
            ],
        )
        connection.execute(
            update(JobAttempt)
            .where(JobAttempt.id == reservation.attempt_id)
            .values(
                phase="succeeded",
                finished_at=finished,
                updated_at=max(finished, attempt["updated_at"]),
                result_manifest_key=bundle.manifest_key,
                result_manifest_sha256=bundle.manifest_sha256,
            )
        )
        changed = connection.scalar(
            update(Job)
            .where(
                Job.id == reservation.job_id,
                Job.execution_backend == "queued",
                Job.status == "processing",
            )
            .values(status="completed", updated_at=func.max(finished, Job.updated_at))
            .returning(Job.id)
        )
        if changed is None:
            raise RuntimeError("Publication could not complete its processing job")
        return True
