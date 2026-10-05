"""Verify durable local results before a short guarded database publication."""

from uuid import UUID

from sqlalchemy import Engine

from app.repositories.job_reservations import JobReservation, publish_verified_result
from app.results import (
    FAKE_RESULT_PROFILE,
    LocalResultStore,
    ResultIdentity,
    ResultProfile,
)


def publish_result(
    engine: Engine,
    reservation: JobReservation,
    *,
    invocation_id: UUID,
    store: LocalResultStore,
    profile: ResultProfile = FAKE_RESULT_PROFILE,
    busy_timeout_ms: int = 1000,
) -> bool:
    """Verify outside the write lock, then check authority and commit publication.

    This trusted internal operation is not a worker endpoint or execution grant.
    Call only after execution has ended. A saved manifest alone is not termination
    evidence. Repeating publication never starts a worker or repeats inference.
    """
    identity = ResultIdentity(
        job_id=reservation.job_id,
        attempt_id=reservation.attempt_id,
        invocation_id=invocation_id,
    )
    bundle = store.verify_bundle(identity, profile=profile)
    return publish_verified_result(
        engine,
        reservation,
        invocation_id=invocation_id,
        bundle=bundle,
        busy_timeout_ms=busy_timeout_ms,
    )
