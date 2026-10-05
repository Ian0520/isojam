"""Run one explicit local dispatch cycle, independently of the API."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

from sqlalchemy import Engine

from app.execution import (
    ExecutionAdapter,
    LocalFakeWorkerAdapter,
    WorkerInvocation,
    WorkerLaunchError,
)
from app.repositories import job_reservations as reservations


@dataclass(frozen=True)
class DispatchResult:
    status: str
    job_id: UUID | None = None
    attempt_id: UUID | None = None
    invocation_id: UUID | None = None
    worker_pid: int | None = None
    manifest_key: str | None = None


def dispatch_once(
    engine: Engine,
    adapter: ExecutionAdapter,
    *,
    dispatcher_id: UUID,
    reservation_ttl_seconds: int = 60,
    authorization_ttl_seconds: int = 300,
    busy_timeout_ms: int = 1000,
) -> DispatchResult:
    """Commit authority before one delivery; never infer/retry/free capacity.

    Idle includes no eligible work or an occupied slot. A launch error preserves
    submitting/running evidence for later reconciliation. A verified fake bundle
    leaves processing/running state intact; process exit is not publication.
    """
    # Validate both lifetimes before reservation can persist any work.
    for name, value in (
        ("reservation_ttl_seconds", reservation_ttl_seconds),
        ("authorization_ttl_seconds", authorization_ttl_seconds),
    ):
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError(f"{name} must be an integer from 1 to 3600")
    token = reservations.reserve_next_job(
        engine,
        dispatcher_id=dispatcher_id,
        reservation_ttl_seconds=reservation_ttl_seconds,
        busy_timeout_ms=busy_timeout_ms,
    )
    if token is None:
        return DispatchResult("idle")
    if not reservations.begin_submission(
        engine,
        token,
        authorization_ttl_seconds=authorization_ttl_seconds,
        busy_timeout_ms=busy_timeout_ms,
    ):
        return DispatchResult("submission_denied", token.job_id, token.attempt_id)
    invocation = WorkerInvocation.from_reservation(token, uuid4())
    # Both transactions have committed and released their connections before
    # this external boundary. No write lock spans worker launch or waiting.
    try:
        report = adapter.submit(invocation)
    except WorkerLaunchError:
        return DispatchResult(
            "submission_unresolved",
            token.job_id,
            token.attempt_id,
            invocation.invocation_id,
        )
    return DispatchResult(
        "worker_" + report.status,
        token.job_id,
        token.attempt_id,
        invocation.invocation_id,
        report.worker_pid,
        report.manifest_key,
    )


def _integer_range(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as error:
            raise argparse.ArgumentTypeError("must be an integer") from error
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(f"must be from {minimum} to {maximum}")
        return parsed

    return parse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.add_argument("--adapter", choices=["local-fake"], required=True)
    parser.add_argument(
        "--worker-timeout-seconds", type=_integer_range(1, 60), default=30
    )
    parser.add_argument(
        "--reservation-ttl-seconds", type=_integer_range(1, 3600), default=60
    )
    parser.add_argument(
        "--authorization-ttl-seconds", type=_integer_range(1, 3600), default=300
    )
    parser.add_argument(
        "--busy-timeout-ms", type=_integer_range(0, 30000), default=1000
    )
    args = parser.parse_args()
    # Commands do not start FastAPI's lifespan or load an inference model.
    from app.database import engine

    try:
        adapter = LocalFakeWorkerAdapter(
            engine, timeout_seconds=args.worker_timeout_seconds
        )
        result = dispatch_once(
            engine,
            adapter,
            dispatcher_id=uuid4(),
            reservation_ttl_seconds=args.reservation_ttl_seconds,
            authorization_ttl_seconds=args.authorization_ttl_seconds,
            busy_timeout_ms=args.busy_timeout_ms,
        )
    except reservations.ReservationBusyError:
        print(json.dumps({"status": "database_busy"}))
        return 75
    except Exception:
        print(
            "Dispatcher operation failed; inspect existing attempt before retry",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()
    print(json.dumps(asdict(result), default=str))
    return 0 if result.status in {"idle", "worker_result_ready"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
