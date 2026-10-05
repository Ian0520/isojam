import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.exc import OperationalError

from app.db_models import Job, JobAttempt

TERMINAL_ATTEMPT_PHASES = frozenset({"succeeded", "failed"})


class ReservationBusyError(RuntimeError):
    """SQLite contention prevented reservation; the caller may try again later."""


@dataclass(frozen=True)
class JobReservation:
    job_id: UUID
    attempt_id: UUID
    attempt_number: int


@contextmanager
def _reservation_transaction(
    engine: Engine, busy_timeout_ms: int
) -> Iterator[Connection]:
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
    engine: Engine, *, busy_timeout_ms: int = 1000
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
    identifiers only after the commit succeeds.
    """
    if engine.dialect.name != "sqlite":
        raise ValueError("Job reservation currently supports SQLite only")
    if type(busy_timeout_ms) is not int or not 0 <= busy_timeout_ms <= 30000:
        raise ValueError("busy_timeout_ms must be an integer between 0 and 30000")

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
        connection.execute(
            insert(JobAttempt).values(
                id=attempt_id,
                job_id=job_id,
                attempt_number=1,
                phase="reserved",
            )
        )
        return JobReservation(job_id, attempt_id, attempt_number=1)
