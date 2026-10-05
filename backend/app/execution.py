"""Trusted, same-host fake execution transport; no hosted provider integration."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine

from app.repositories.job_reservations import JobReservation

MAX_CONTROL_MESSAGE_BYTES = 4096
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
WorkerStatus = Literal["contact_recorded", "permission_denied", "heartbeat_rejected"]
WORKER_EXIT_CODES = {
    "contact_recorded": 0,
    "permission_denied": 3,
    "heartbeat_rejected": 4,
}


class WorkerInvocation(BaseModel):
    """One local delivery; identifiers are coordination data, not credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    job_id: UUID
    attempt_id: UUID
    attempt_number: PositiveInt
    dispatcher_id: UUID
    dispatcher_generation: PositiveInt
    reservation_expires_at: AwareDatetime
    invocation_id: UUID

    @classmethod
    def from_reservation(cls, reservation: JobReservation, invocation_id: UUID):
        return cls(
            job_id=reservation.job_id,
            attempt_id=reservation.attempt_id,
            attempt_number=reservation.attempt_number,
            dispatcher_id=reservation.dispatcher_id,
            dispatcher_generation=reservation.dispatcher_generation,
            reservation_expires_at=reservation.reservation_expires_at,
            invocation_id=invocation_id,
        )

    def reservation(self) -> JobReservation:
        return JobReservation(
            self.job_id,
            self.attempt_id,
            self.attempt_number,
            self.dispatcher_id,
            self.dispatcher_generation,
            self.reservation_expires_at,
        )


class WorkerReport(BaseModel):
    """Contact/denial evidence only, never a completed inference result."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    job_id: UUID
    attempt_id: UUID
    invocation_id: UUID
    worker_pid: PositiveInt
    status: WorkerStatus


class WorkerLaunchError(RuntimeError):
    """Delivery or its report is unresolved; never permission to submit again."""


class ExecutionAdapter(Protocol):
    def submit(self, invocation: WorkerInvocation) -> WorkerReport: ...


class LocalFakeWorkerAdapter:
    """Run one trusted fake worker on the SQLite host, with bounded waiting.

    This synchronous adapter is a local contract exercise. It is not a GPU
    provider submit/inspect/cancel adapter. It neither retries nor retires work.
    The child has no descendants; run() kills and waits for it on timeout.
    """

    def __init__(self, engine: Engine, *, timeout_seconds: int = 30):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be an integer from 1 to 60")
        database = engine.url.database
        if (
            engine.dialect.name != "sqlite"
            or not database
            or database == ":memory:"
            or engine.url.query
        ):
            raise ValueError(
                "Local fake execution requires a file-backed SQLite engine"
            )
        path = Path(database)
        if not path.is_absolute():
            raise ValueError("Local fake execution requires an absolute database path")
        self.database_path = path.resolve()
        self.timeout_seconds = timeout_seconds

    def submit(self, invocation: WorkerInvocation) -> WorkerReport:
        worker_env = os.environ.copy()
        worker_env["ISOJAM_DATABASE_PATH"] = str(self.database_path)
        # Worker coordination does not use the user's login signing secret.
        worker_env.pop("ISOJAM_JWT_SECRET_KEY", None)
        payload = invocation.model_dump_json().encode("utf-8")
        if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
            raise WorkerLaunchError("Local invocation exceeds the message limit")
        with TemporaryFile() as output:
            try:
                result = subprocess.run(
                    [sys.executable, "-m", "app.fake_worker"],
                    input=payload,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=worker_env,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise WorkerLaunchError(
                    "Local fake worker launch/report is unresolved"
                ) from error
            output.seek(0)
            raw = output.read(MAX_CONTROL_MESSAGE_BYTES + 1)
        if len(raw) > MAX_CONTROL_MESSAGE_BYTES:
            raise WorkerLaunchError(
                "Local fake worker report exceeds the message limit"
            )
        try:
            report = WorkerReport.model_validate_json(raw)
        except ValidationError as error:
            raise WorkerLaunchError(
                "Local fake worker returned an invalid report"
            ) from error
        if (report.job_id, report.attempt_id, report.invocation_id) != (
            invocation.job_id,
            invocation.attempt_id,
            invocation.invocation_id,
        ) or result.returncode != WORKER_EXIT_CODES[report.status]:
            raise WorkerLaunchError(
                "Local fake worker report does not match its invocation"
            )
        return report
