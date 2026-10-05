"""One local fake invocation: authorization, contact and durable dummy WAVs."""

import argparse
import os
import sys
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import Engine

from app.config import get_audio_storage_dir
from app.execution import (
    MAX_CONTROL_MESSAGE_BYTES,
    WORKER_EXIT_CODES,
    WorkerInvocation,
    WorkerReport,
)
from app.repositories import job_reservations as reservations
from app.results import FAKE_RESULT_PROFILE, LocalResultStore


def create_fake_workspace(workspace: Path) -> None:
    """Seven deterministic tiny WAVs; these are test artifacts, not separation."""
    for index, stem in enumerate(FAKE_RESULT_PROFILE.required_stems, start=1):
        with wave.open(str(workspace / f"{stem}.wav"), "wb") as audio:
            audio.setnchannels(2)
            audio.setsampwidth(2)
            audio.setframerate(44100)
            audio.writeframes(index.to_bytes(2, "little", signed=True) * 16 * 2)


def run_fake_worker(
    engine: Engine,
    invocation: WorkerInvocation,
    *,
    result_store: LocalResultStore | None = None,
) -> WorkerReport:
    store = result_store or LocalResultStore(get_audio_storage_dir() / "results")
    manifest_key = None
    token = invocation.reservation()
    # A repeated grant is denied even for the same invocation. Never proceed
    # when permission fails, raises, or its acknowledgement was lost.
    if not reservations.authorize_execution(
        engine,
        token,
        invocation_id=invocation.invocation_id,
        local_worker_pid=os.getpid(),
        local_worker_id=invocation.local_worker_id or uuid4(),
    ):
        status = "permission_denied"
    elif reservations.record_heartbeat(
        engine, token, invocation_id=invocation.invocation_id
    ):
        with TemporaryDirectory(prefix="isojam-fake-") as temporary:
            workspace = Path(temporary)
            create_fake_workspace(workspace)
            bundle = store.write_bundle(invocation.result_identity(), workspace)
        manifest_key = bundle.manifest_key
        status = "result_ready"
    else:
        status = "heartbeat_rejected"
    return WorkerReport(
        job_id=invocation.job_id,
        attempt_id=invocation.attempt_id,
        invocation_id=invocation.invocation_id,
        worker_pid=os.getpid(),
        status=status,
        manifest_key=manifest_key,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    raw = sys.stdin.buffer.read(MAX_CONTROL_MESSAGE_BYTES + 1)
    try:
        if len(raw) > MAX_CONTROL_MESSAGE_BYTES:
            raise ValueError("oversized request")
        invocation = WorkerInvocation.model_validate_json(raw)
    except (ValidationError, ValueError):
        print("Invalid local worker request", file=sys.stderr)
        return 2
    # Validate the request before opening the configured database. Importing this
    # module or asking for --help must not initialize application data/model state.
    from app.database import engine

    try:
        report = run_fake_worker(engine, invocation)
    except Exception:
        print(
            "Local worker control operation failed; reconciliation required",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()
    print(report.model_dump_json())
    return WORKER_EXIT_CODES[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
