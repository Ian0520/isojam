import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, insert, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.sql.elements import ColumnElement

from app.db_models import Job, JobAttempt

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


def _current_pending_queued_attempt() -> ColumnElement[bool]:
    eligible_job = (
        select(Job.id)
        .where(
            Job.id == JobAttempt.job_id,
            Job.execution_backend == "queued",
            Job.status == "pending",
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
                _current_pending_queued_attempt(),
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
    engine: Engine, reservation: JobReservation, *, busy_timeout_ms: int = 1000
) -> bool:
    """Commit submission intent once for a current, unexpired reservation.

    True is returned only after the transition commits. Only that successful
    caller may make the initial external submission. A duplicate/stale call
    returns False; a commit error raises. Never contact a worker inside the
    transaction. A crash or ambiguous response after intent requires reconciliation,
    not another call to submit. This is not worker execution authorization.
    """
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
                _current_pending_queued_attempt(),
            )
            .values(phase="submitting", updated_at=now)
            .returning(JobAttempt.id)
        )
        return changed is not None
