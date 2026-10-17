"""One authorized same-host CUDA invocation, with diagnostics on stderr."""

import argparse
import logging
import os
import sys
from threading import Event, Thread

from pydantic import ValidationError
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker

from app.config import get_audio_storage_dir
from app.execution import (
    MAX_CONTROL_MESSAGE_BYTES,
    WORKER_EXIT_CODES,
    WorkerInvocation,
    WorkerReport,
)
from app.model import create_model_session
from app.model_processing import process_model_job
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore

logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 15


def run_model_worker(
    engine: Engine,
    invocation: WorkerInvocation,
    *,
    model_session_factory=create_model_session,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> WorkerReport:
    """Acquire permission before model loading; the caller must confirm exit."""
    if invocation.local_worker_id is None:
        raise ValueError("Model worker requires a controller-assigned launch identity")
    if not 0 < heartbeat_interval_seconds <= 60:
        raise ValueError("heartbeat interval must be positive and at most 60 seconds")
    token = invocation.reservation()

    def report(status, manifest_key=None):
        return WorkerReport(
            job_id=invocation.job_id,
            attempt_id=invocation.attempt_id,
            invocation_id=invocation.invocation_id,
            worker_pid=os.getpid(),
            status=status,
            manifest_key=manifest_key,
        )

    if not reservations.authorize_execution(
        engine,
        token,
        invocation_id=invocation.invocation_id,
        local_worker_pid=os.getpid(),
        local_worker_id=invocation.local_worker_id,
    ):
        return report("permission_denied")
    if not reservations.record_heartbeat(
        engine, token, invocation_id=invocation.invocation_id
    ):
        return report("heartbeat_rejected")

    stopped = Event()
    rejected = Event()

    def heartbeat():
        while not stopped.wait(heartbeat_interval_seconds):
            try:
                if not reservations.record_heartbeat(
                    engine, token, invocation_id=invocation.invocation_id
                ):
                    rejected.set()
                    return
            except Exception:
                # Transient contact failure does not revoke the original grant,
                # nor does it authorize anyone else to repeat this inference.
                logger.exception("Worker heartbeat could not be recorded")

    contact = Thread(target=heartbeat, name="isojam-heartbeat", daemon=True)
    contact.start()
    model_session = None
    try:
        model_session = model_session_factory()
        if rejected.is_set():
            return report("heartbeat_rejected")
        root = get_audio_storage_dir()
        bundle = process_model_job(
            invocation.result_identity(),
            model_session,
            sessionmaker(engine),
            upload_dir=root / "uploads",
            result_store=LocalResultStore(root / "results"),
        )
        if rejected.is_set():
            return report("heartbeat_rejected")
        return report("result_ready", bundle.manifest_key)
    finally:
        try:
            if model_session is not None:
                model_session.close()
        finally:
            stopped.set()
            contact.join(timeout=5)
            if contact.is_alive():
                raise RuntimeError("Worker heartbeat thread did not stop")


def main(*, model_session_factory=create_model_session) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_CONTROL_MESSAGE_BYTES + 1)
    try:
        if len(raw) > MAX_CONTROL_MESSAGE_BYTES:
            raise ValueError("oversized request")
        invocation = WorkerInvocation.model_validate_json(raw)
    except (ValidationError, ValueError):
        print("Invalid local model worker request", file=sys.stderr)
        return 2

    # Preserve a private control descriptor, then redirect OS stdout too: native
    # Torch/library writes must not corrupt the single JSON reply on the old fd.
    sys.stdout.flush()
    control_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    logging.basicConfig(level=logging.INFO)
    with os.fdopen(control_fd, "w", encoding="utf-8") as control:
        from app.database import engine

        try:
            report = run_model_worker(
                engine, invocation, model_session_factory=model_session_factory
            )
        except Exception:
            logger.exception("Local model worker failed; inspect invocation log")
            return 1
        finally:
            engine.dispose()
        print(report.model_dump_json(), file=control, flush=True)
        return WORKER_EXIT_CODES[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
