"""Synchronous Linux execution for the pinned, same-process CUDA model."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryFile
from uuid import uuid4

from sqlalchemy import Engine

from app.config import get_audio_storage_dir, get_max_upload_bytes
from app.execution import (
    MAX_CONTROL_MESSAGE_BYTES,
    WorkerExecutionFailed,
    WorkerInvocation,
    WorkerLaunchError,
    WorkerReport,
    parse_worker_report,
)
from app.model_processing import get_model_result_profile
from app.repositories.job_reservations import (
    ReservationBusyError,
    record_execution_stopped,
)
from app.results import LocalResultStore, ResultValidationError

DEFAULT_MODEL_TIMEOUT_SECONDS = 1800
STOP_TIMEOUT_SECONDS = 5


class LocalModelWorkerAdapter:
    """One new process/session per job; never load Torch in the controller.

    The pinned eager inference path runs CUDA in the direct child, without child
    processes or distributed/compiled execution. A surviving process-group member
    prevents stop proof. This is a trusted same-host runner, not an isolation
    boundary for arbitrary model code or descendants that escape their session.
    """

    def __init__(
        self, engine: Engine, *, timeout_seconds: int = DEFAULT_MODEL_TIMEOUT_SECONDS
    ):
        if sys.platform != "linux":
            raise ValueError("Local model execution requires Linux")
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 7200:
            raise ValueError("timeout_seconds must be an integer from 1 to 7200")
        database = engine.url.database
        if (
            engine.dialect.name != "sqlite"
            or not database
            or database == ":memory:"
            or engine.url.query
            or not Path(database).is_absolute()
        ):
            raise ValueError(
                "Local model execution requires an absolute file-backed SQLite database"
            )
        self.engine = engine
        self.database_path = Path(database).resolve()
        self.timeout_seconds = timeout_seconds
        self.audio_storage_dir = get_audio_storage_dir()
        self.profile = get_model_result_profile()
        self.max_upload_bytes = get_max_upload_bytes()
        self.results = LocalResultStore(self.audio_storage_dir / "results")

    @staticmethod
    def _group_gone(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        return False

    @staticmethod
    def _stop(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise WorkerLaunchError(
                "Model worker termination is unconfirmed"
            ) from error

    def submit(self, invocation: WorkerInvocation) -> WorkerReport:
        local_worker_id = uuid4()
        delivery = invocation.model_copy(update={"local_worker_id": local_worker_id})
        payload = delivery.model_dump_json().encode("utf-8")
        if len(payload) > MAX_CONTROL_MESSAGE_BYTES:
            raise WorkerLaunchError("Local model invocation exceeds the message limit")
        worker_env = os.environ.copy()
        worker_env.update(
            ISOJAM_DATABASE_PATH=str(self.database_path),
            ISOJAM_AUDIO_STORAGE_DIR=str(self.audio_storage_dir),
            ISOJAM_MAX_AUDIO_DURATION_SECONDS=str(self.profile.max_duration_seconds),
            ISOJAM_MAX_UPLOAD_BYTES=str(self.max_upload_bytes),
            PYTHONUNBUFFERED="1",
        )
        worker_env.pop("ISOJAM_JWT_SECRET_KEY", None)
        log_dir = self.audio_storage_dir / "worker-logs"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = log_dir / f"{invocation.invocation_id}-{local_worker_id}.log"
        try:
            log_fd = os.open(
                log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(log_fd, "wb") as log, TemporaryFile() as output:
                process = subprocess.Popen(
                    [sys.executable, "-m", "app.model_worker"],
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=log,
                    env=worker_env,
                    start_new_session=True,
                )
                timed_out = False
                try:
                    process.communicate(input=payload, timeout=self.timeout_seconds)
                except subprocess.TimeoutExpired:
                    self._stop(process)
                    timed_out = True
                except BaseException:
                    self._stop(process)
                    raise
                # Waiting for the direct child alone is insufficient if an
                # unexpected descendant still belongs to its execution group.
                if not self._group_gone(process.pid):
                    raise WorkerLaunchError("Model worker process group has not ended")
                try:
                    stopped = record_execution_stopped(
                        self.engine,
                        invocation.reservation(),
                        invocation_id=invocation.invocation_id,
                        exit_code=process.returncode,
                        local_worker_pid=process.pid,
                        local_worker_id=local_worker_id,
                    )
                except ReservationBusyError:
                    raise
                except Exception as error:
                    raise WorkerLaunchError(
                        "Model worker stop evidence could not be committed"
                    ) from error
                if stopped and (timed_out or process.returncode != 0):
                    raise WorkerExecutionFailed(
                        "Model worker ended unsuccessfully; reconcile saved results"
                    )
                output.seek(0)
                raw = output.read(MAX_CONTROL_MESSAGE_BYTES + 1)
        except OSError as error:
            raise WorkerLaunchError(
                "Local model worker launch/termination is unresolved"
            ) from error

        report = parse_worker_report(
            raw, invocation, worker_pid=process.pid, exit_code=process.returncode
        )
        if report.status == "result_ready":
            identity = invocation.result_identity()
            if not stopped or report.manifest_key != self.results.manifest_key(
                identity
            ):
                raise WorkerLaunchError(
                    "Model result/stop evidence does not match its invocation"
                )
            try:
                self.results.verify_bundle(identity, profile=self.profile)
            except ResultValidationError as error:
                raise WorkerLaunchError(
                    "Model result bundle could not be verified"
                ) from error
        return report
