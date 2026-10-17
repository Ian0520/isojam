"""Run queued dispatch cycles independently of the API, once or continuously."""

import argparse
import json
import logging
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

from sqlalchemy import Engine

from app.execution import (
    ExecutionAdapter,
    LocalFakeWorkerAdapter,
    WorkerExecutionFailed,
    WorkerInvocation,
    WorkerLaunchError,
)
from app.model_execution import LocalModelWorkerAdapter
from app.publication import publish_result
from app.repositories import job_reservations as reservations
from app.results import (
    FAKE_RESULT_PROFILE,
    LocalResultStore,
    ResultIdentity,
    ResultProfile,
    ResultValidationError,
)

logger = logging.getLogger(__name__)


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
    profile: ResultProfile,
    busy_timeout_ms: int,
) -> str:
    """Keep normal delivery and recovery on the same verification/commit boundary."""
    try:
        published = publish_result(
            engine,
            token,
            invocation_id=invocation_id,
            store=store,
            profile=profile,
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
    profile: ResultProfile = FAKE_RESULT_PROFILE,
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
        profile=profile,
        busy_timeout_ms=busy_timeout_ms,
    )
    if status == "result_invalid" and candidate.execution_exit_code != 0:
        try:
            if not store.has_manifest(identity):
                failed = reservations.fail_stopped_attempt(
                    engine,
                    token,
                    invocation_id=candidate.invocation_id,
                    busy_timeout_ms=busy_timeout_ms,
                )
                status = "failed" if failed else "failure_denied"
        except reservations.ReservationBusyError:
            raise
        except ResultValidationError:
            pass  # Unreadable storage is not evidence that no result exists.
        except Exception:
            status = "failure_unresolved"
    return DispatchResult(
        "recovered" if status == "completed" else status,
        token.job_id,
        token.attempt_id,
        candidate.invocation_id,
        candidate.worker_pid,
        None
        if status in {"failed", "failure_denied", "failure_unresolved"}
        else store.manifest_key(identity),
    )


def dispatch_once(
    engine: Engine,
    adapter: ExecutionAdapter,
    *,
    dispatcher_id: UUID,
    profile: ResultProfile = FAKE_RESULT_PROFILE,
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
    if not isinstance(profile, ResultProfile):
        raise ValueError("profile must be a trusted ResultProfile")
    for name, value in (
        ("reservation_ttl_seconds", reservation_ttl_seconds),
        ("authorization_ttl_seconds", authorization_ttl_seconds),
    ):
        if type(value) is not int or not 1 <= value <= 3600:
            raise ValueError(f"{name} must be an integer from 1 to 3600")
    recovered = reconcile_once(
        engine, adapter.results, profile=profile, busy_timeout_ms=busy_timeout_ms
    )
    if recovered is not None:
        return recovered
    token = reservations.reserve_next_job(
        engine,
        dispatcher_id=dispatcher_id,
        reservation_ttl_seconds=reservation_ttl_seconds,
        busy_timeout_ms=busy_timeout_ms,
        reclaim_expired=True,
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
    except WorkerExecutionFailed:
        return reconcile_once(
            engine, adapter.results, profile=profile, busy_timeout_ms=busy_timeout_ms
        ) or DispatchResult(
            "failure_unresolved",
            token.job_id,
            token.attempt_id,
            invocation.invocation_id,
        )
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
            profile=profile,
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


def run_dispatch_loop(
    engine: Engine,
    adapter: ExecutionAdapter,
    *,
    dispatcher_id: UUID,
    should_stop: Callable[[], bool],
    on_result: Callable[[DispatchResult], None],
    profile: ResultProfile = FAKE_RESULT_PROFILE,
    poll_interval_seconds: float = 2,
    reservation_ttl_seconds: int = 60,
    authorization_ttl_seconds: int = 300,
    busy_timeout_ms: int = 1000,
) -> None:
    """Drain one cycle at a time; polling never grants another execution.

    Finish an active cycle before stopping. Idle/errors wait between cycles;
    completed/recovered/failed work permits the next cycle immediately. Duplicate
    reports are suppressed, but every cycle still rechecks the durable database.
    Unexpected exceptions exit to the supervisor instead of hiding a broken loop.
    """
    if (
        type(poll_interval_seconds) not in {int, float}
        or not 0.1 <= poll_interval_seconds <= 60
    ):
        raise ValueError("poll interval must be from 0.1 to 60 seconds")
    previous = None
    while not should_stop():
        try:
            result = dispatch_once(
                engine,
                adapter,
                dispatcher_id=dispatcher_id,
                profile=profile,
                reservation_ttl_seconds=reservation_ttl_seconds,
                authorization_ttl_seconds=authorization_ttl_seconds,
                busy_timeout_ms=busy_timeout_ms,
            )
        except reservations.ReservationBusyError:
            result = DispatchResult("database_busy")
        if result != previous:
            on_result(result)
            previous = result
        if (
            result.status not in {"completed", "recovered", "failed"}
            and not should_stop()
        ):
            time.sleep(poll_interval_seconds)


@contextmanager
def _shutdown_signals() -> Iterator[Callable[[], bool]]:
    stopping = False

    def request_stop(signum, frame):
        # A plain flag avoids acquiring locks or performing I/O in a signal handler.
        nonlocal stopping
        stopping = True

    previous = {
        number: signal.signal(number, request_stop)
        for number in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield lambda: stopping
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _print_result(result: DispatchResult) -> None:
    print(json.dumps(asdict(result), default=str), flush=True)


def _poll_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not 0.1 <= interval <= 60:
        raise argparse.ArgumentTypeError("must be from 0.1 to 60 seconds")
    return interval


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
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument(
        "--serve",
        action="store_true",
        help="Poll continuously; drain the active cycle on SIGTERM/SIGINT",
    )
    parser.add_argument(
        "--adapter", choices=["local-fake", "local-model"], required=True
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Recover at most one stopped attempt without launching a worker",
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=_integer_range(1, 7200),
        help="Default: 30 for local-fake, 1800 for local-model (model maximum: 7200)",
    )
    parser.add_argument("--poll-interval-seconds", type=_poll_interval, default=2)
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
    if args.serve and args.reconcile_only:
        parser.error("--reconcile-only requires --once")
    if (
        args.adapter == "local-fake"
        and args.worker_timeout_seconds is not None
        and args.worker_timeout_seconds > 60
    ):
        parser.error("local-fake worker timeout must be from 1 to 60 seconds")
    # Commands do not start FastAPI's lifespan or load an inference model.
    from app.database import engine

    logging.basicConfig(level=logging.INFO)
    try:
        adapter_type = (
            LocalFakeWorkerAdapter
            if args.adapter == "local-fake"
            else LocalModelWorkerAdapter
        )
        adapter = (
            adapter_type(engine)
            if args.worker_timeout_seconds is None
            else adapter_type(engine, timeout_seconds=args.worker_timeout_seconds)
        )
        profile = (
            FAKE_RESULT_PROFILE if args.adapter == "local-fake" else adapter.profile
        )
        if args.serve:
            with _shutdown_signals() as should_stop:
                run_dispatch_loop(
                    engine,
                    adapter,
                    dispatcher_id=uuid4(),
                    should_stop=should_stop,
                    on_result=_print_result,
                    profile=profile,
                    poll_interval_seconds=args.poll_interval_seconds,
                    reservation_ttl_seconds=args.reservation_ttl_seconds,
                    authorization_ttl_seconds=args.authorization_ttl_seconds,
                    busy_timeout_ms=args.busy_timeout_ms,
                )
            _print_result(DispatchResult("stopped"))
            return 0
        if args.reconcile_only:
            result = reconcile_once(
                engine,
                adapter.results,
                profile=profile,
                busy_timeout_ms=args.busy_timeout_ms,
            ) or DispatchResult("idle")
        else:
            result = dispatch_once(
                engine,
                adapter,
                dispatcher_id=uuid4(),
                profile=profile,
                reservation_ttl_seconds=args.reservation_ttl_seconds,
                authorization_ttl_seconds=args.authorization_ttl_seconds,
                busy_timeout_ms=args.busy_timeout_ms,
            )
    except reservations.ReservationBusyError:
        print(json.dumps({"status": "database_busy"}))
        return 75
    except Exception:
        logger.exception(
            "Dispatcher operation failed; inspect existing attempt before retry"
        )
        return 1
    finally:
        engine.dispose()
    _print_result(result)
    return 0 if result.status in {"idle", "completed", "recovered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
