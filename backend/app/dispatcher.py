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
from app.publication import publish_result
from app.repositories import job_reservations as reservations
from app.results import LocalResultStore, ResultIdentity, ResultValidationError


@dataclass(frozen=True)
class DispatchResult:
    status: str
    job_id: UUID | None = None
    attempt_id: UUID | None = None
    invocation_id: UUID | None = None
    worker_pid: int | None = None
    manifest_key: str | None = None


def _publication_status(
    engine: Engine,
    token: reservations.JobReservation,
    invocation_id: UUID,
    store: LocalResultStore,
    *,
    busy_timeout_ms: int,
) -> str:
    """Keep normal delivery and recovery on the same verification/commit boundary."""
    try:
        published = publish_result(
            engine,
            token,
            invocation_id=invocation_id,
            store=store,
            busy_timeout_ms=busy_timeout_ms,
        )
    except ResultValidationError:
        return "result_invalid"
    except reservations.PublicationConflictError:
        return "publication_conflict"
    except reservations.ReservationBusyError:
        raise
    except Exception:
        # Preserve files and occupied state. A later cycle may retry publication.
        return "publication_unresolved"
    return "completed" if published else "publication_denied"


def reconcile_once(
    engine: Engine,
    store: LocalResultStore,
    *,
    busy_timeout_ms: int = 1000,
) -> DispatchResult | None:
    """Recover one saved bundle from a stopped invocation without launching work.

    None means no eligible candidate, including held attempts without exit proof.
    Any candidate outcome ends this pass; invalid/unresolved results never cause
    a fall-through to a new execution. The stored owner/generation are a snapshot
    for publication rechecking, not ownership transferred to this controller.
    """
    candidate = reservations.find_recoverable_attempt(
        engine,
        busy_timeout_ms=busy_timeout_ms,
    )
    if candidate is None:
        return None
    token = candidate.reservation
    identity = ResultIdentity(
        job_id=token.job_id,
        attempt_id=token.attempt_id,
        invocation_id=candidate.invocation_id,
    )
    status = _publication_status(
        engine,
        token,
        candidate.invocation_id,
        store,
        busy_timeout_ms=busy_timeout_ms,
    )
    return DispatchResult(
        "recovered" if status == "completed" else status,
        token.job_id,
        token.attempt_id,
        candidate.invocation_id,
        candidate.worker_pid,
        store.manifest_key(identity),
    )


def dispatch_once(
    engine: Engine,
    adapter: ExecutionAdapter,
    *,
    dispatcher_id: UUID,
    reservation_ttl_seconds: int = 60,
    authorization_ttl_seconds: int = 300,
    busy_timeout_ms: int = 1000,
) -> DispatchResult:
    """Recover one stopped attempt, or dispatch one new job and publish its result.

    Idle includes no eligible work or an occupied slot. A launch error preserves
    submitting/running evidence for later reconciliation. Only a verified bundle
    from an ended execution can publish output rows and completion together.
    """
    # Validate settings before recovery or reservation can persist any work.
    if not isinstance(dispatcher_id, UUID):
        raise ValueError("dispatcher_id must be a UUID")
    for name, value in (
        ("reservation_ttl_seconds", reservation_ttl_seconds),
        ("authorization_ttl_seconds", authorization_ttl_seconds),
    ):
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError(f"{name} must be an integer from 1 to 3600")
    recovered = reconcile_once(engine, adapter.results, busy_timeout_ms=busy_timeout_ms)
    if recovered is not None:
        return recovered
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
    status = "worker_" + report.status
    if report.status == "result_ready":
        status = _publication_status(
            engine,
            token,
            invocation.invocation_id,
            adapter.results,
            busy_timeout_ms=busy_timeout_ms,
        )
    return DispatchResult(
        status,
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
        "--reconcile-only",
        action="store_true",
        help="Recover at most one stopped attempt without launching a worker",
    )
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
        if args.reconcile_only:
            result = reconcile_once(
                engine,
                adapter.results,
                busy_timeout_ms=args.busy_timeout_ms,
            ) or DispatchResult("idle")
        else:
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
    return 0 if result.status in {"idle", "completed", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
