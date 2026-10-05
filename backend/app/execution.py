"""Trusted, same-host fake execution transport; no hosted provider integration."""

import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from sqlalchemy import Engine

from app.config import get_audio_storage_dir
from app.repositories.job_reservations import (
    JobReservation,
    ReservationBusyError,
    record_execution_stopped,
)
from app.results import LocalResultStore, ResultIdentity, ResultValidationError

MAX_CONTROL_MESSAGE_BYTES = 4096
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
WorkerStatus = Literal["result_ready", "permission_denied", "heartbeat_rejected"]
WORKER_EXIT_CODES = {
    "result_ready": 0,
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
    local_worker_id: UUID | None = None

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

    def result_identity(self) -> ResultIdentity:
        return ResultIdentity(
            job_id=self.job_id,
            attempt_id=self.attempt_id,
            invocation_id=self.invocation_id,
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
    """Bundle/denial evidence only; database publication remains separate."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    job_id: UUID
    attempt_id: UUID
    invocation_id: UUID
    worker_pid: PositiveInt
    status: WorkerStatus
    manifest_key: Annotated[str, Field(min_length=1, max_length=256)] | None = None

    @model_validator(mode="after")
    def manifest_matches_status(self):
        if (self.status == "result_ready") != (self.manifest_key is not None):
            raise ValueError("Only result_ready reports require a manifest key")
        return self


class WorkerLaunchError(RuntimeError):
    """Delivery or its report is unresolved; never permission to submit again."""


class ExecutionAdapter(Protocol):
    """Trusted synchronous delivery: confirm and persist execution stop before success.

    An asynchronous provider acknowledgement cannot fulfill this local contract.
    """

    results: LocalResultStore

    def submit(self, invocation: WorkerInvocation) -> WorkerReport: ...


class LocalFakeWorkerAdapter:
    """Run one trusted fake worker on the SQLite host, with bounded waiting.

    This synchronous adapter is a local contract exercise. It is not a GPU
    provider submit/inspect/cancel adapter. It neither retries nor retires work.
    The child has no descendants; timeout cleanup kills and waits for it.
    Confirmed exit is committed before parsing its report, independently of success.
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
        self.engine = engine
        self.database_path = path.resolve()
        self.timeout_seconds = timeout_seconds
        self.audio_storage_dir = get_audio_storage_dir()
        self.results = LocalResultStore(self.audio_storage_dir / "results")

    def submit(self, invocation: WorkerInvocation) -> WorkerReport:
        worker_env = os.environ.copy()
        worker_env["ISOJAM_DATABASE_PATH"] = str(self.database_path)
        worker_env["ISOJAM_AUDIO_STORAGE_DIR"] = str(self.audio_storage_dir)
        # Worker coordination does not use the user's login signing secret.
        worker_env.pop("ISOJAM_JWT_SECRET_KEY", None)
        # Fresh per launch, even if a caller repeats an invocation. PID namespaces
        # and PID reuse cannot make a different delivery the authorized child.
        local_worker_id = uuid4()
        delivery = invocation.model_copy(update={"local_worker_id": local_worker_id})
        payload = delivery.model_dump_json().encode("utf-8")
        if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
            raise WorkerLaunchError("Local invocation exceeds the message limit")
        with TemporaryFile() as output:
            try:
                timed_out = False
                with subprocess.Popen(
                    [sys.executable, "-m", "app.fake_worker"],
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env=worker_env,
                ) as result:
                    try:
                        result.communicate(input=payload, timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        result.kill()
                        result.communicate()
                        timed_out = True
                    except BaseException:
                        # Do not leave the direct child alive on an interrupted send.
                        # No stop proof is invented if cleanup or persistence fails.
                        result.kill()
                        result.wait()
                        raise
            except OSError as error:
                raise WorkerLaunchError(
                    "Local fake worker launch/report is unresolved"
                ) from error
            try:
                stopped = record_execution_stopped(
                    self.engine,
                    invocation.reservation(),
                    invocation_id=invocation.invocation_id,
                    exit_code=result.returncode,
                    local_worker_pid=result.pid,
                    local_worker_id=local_worker_id,
                )
            except ReservationBusyError:
                raise
            except Exception as error:
                raise WorkerLaunchError(
                    "Local worker ended but its stop evidence could not be committed"
                ) from error
            if timed_out:
                raise WorkerLaunchError("Local fake worker timed out; execution ended")
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
        ) or (
            result.returncode != WORKER_EXIT_CODES[report.status]
            or report.worker_pid != result.pid
        ):
            raise WorkerLaunchError(
                "Local fake worker report does not match its invocation"
            )
        if report.status == "result_ready":
            identity = invocation.result_identity()
            if report.manifest_key != self.results.manifest_key(identity):
                raise WorkerLaunchError(
                    "Worker manifest key does not match its invocation"
                )
            try:
                self.results.verify_bundle(identity)
            except ResultValidationError as error:
                raise WorkerLaunchError(
                    "Worker result bundle could not be verified"
                ) from error
            if not stopped:
                raise WorkerLaunchError(
                    "Worker stop evidence does not match its invocation"
                )
        return report
