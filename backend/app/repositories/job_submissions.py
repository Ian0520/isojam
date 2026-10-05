from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from sqlalchemy.orm import Session

from app.db_models import Job, JobSubmissionReceipt
from app.repositories import jobs


class SubmissionConflictError(ValueError):
    """A submission key already belongs to a different request."""


@dataclass(frozen=True)
class JobSubmission:
    job: Job
    created: bool


def _request_fingerprint(upload_id: UUID) -> str:
    # Canonical UUID text ignores JSON formatting/case. Include every future
    # processing option here before accepting it as part of a job request.
    return sha256(f"create-job:v1:{upload_id}".encode("ascii")).hexdigest()


def get_replayed_job(
    session: Session, user_id: UUID, upload_id: UUID, key: str
) -> Job | None:
    receipt = session.get(JobSubmissionReceipt, (user_id, key))
    if receipt is None:
        return None
    if receipt.request_fingerprint != _request_fingerprint(upload_id):
        raise SubmissionConflictError("Idempotency key belongs to another request")
    return session.get(Job, receipt.job_id)


def create_submission(
    session: Session,
    upload_id: UUID,
    user_id: UUID,
    *,
    key: str | None,
    max_unfinished_jobs: int,
) -> JobSubmission | None:
    """Admit or replay in the caller's transaction; never commit or dispatch.

    The conditional job INSERT is the first write. SQLite serializes writers:
    a competing request waits for the receipt to commit, then inserts no job.
    Re-read its receipt after that statement, even when the quota is now full.
    The caller must commit the new job and receipt together before dispatch.
    """
    job = jobs.create_job_with_limit(
        session,
        upload_id,
        user_id,
        max_unfinished_jobs=max_unfinished_jobs,
        submission_key=key,
    )
    if job is None:
        replay = (
            get_replayed_job(session, user_id, upload_id, key)
            if key is not None
            else None
        )
        return JobSubmission(replay, created=False) if replay is not None else None
    if key is not None:
        session.add(
            JobSubmissionReceipt(
                user_id=user_id,
                key=key,
                request_fingerprint=_request_fingerprint(upload_id),
                job_id=job.id,
            )
        )
        session.flush()
    return JobSubmission(job, created=True)
