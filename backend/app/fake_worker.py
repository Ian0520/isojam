"""One local fake invocation: authorization and contact, without inference."""

import argparse
import os
import sys

from pydantic import ValidationError
from sqlalchemy import Engine

from app.execution import (
    MAX_CONTROL_MESSAGE_BYTES,
    WORKER_EXIT_CODES,
    WorkerInvocation,
    WorkerReport,
)
from app.repositories import job_reservations as reservations


def run_fake_worker(engine: Engine, invocation: WorkerInvocation) -> WorkerReport:
    token = invocation.reservation()
    # A repeated grant is denied even for the same invocation. Never proceed
    # when permission fails, raises, or its acknowledgement was lost.
    if not reservations.authorize_execution(
        engine,
        token,
        invocation_id=invocation.invocation_id,
    ):
        status = "permission_denied"
    elif reservations.record_heartbeat(
        engine, token, invocation_id=invocation.invocation_id
    ):
        status = "contact_recorded"
    else:
        status = "heartbeat_rejected"
    return WorkerReport(
        job_id=invocation.job_id,
        attempt_id=invocation.attempt_id,
        invocation_id=invocation.invocation_id,
        worker_pid=os.getpid(),
        status=status,
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
