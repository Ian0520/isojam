from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from app.db_models import Job, Upload

ALLOWED_STATUSES = {"pending", "processing", "completed", "failed"}


def create_job(session: Session, upload_id: UUID) -> Job:
    new_job = Job(upload_id=upload_id, status="pending")

    session.add(new_job)
    session.flush()
    return new_job


def get_job(session: Session, job_id: UUID) -> Job | None:
    return session.get(Job, job_id)


def update_job_status(
    session: Session,
    job_id: UUID,
    status: str,
) -> Job | None:
    job = get_job(session, job_id)
    if job is None:
        return None
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid job status: {status}")
    job.status = status
    session.flush()
    return job


def create_job_with_limit(
    session: Session,
    upload_id: UUID,
    user_id: UUID,
    *,
    max_unfinished_jobs: int,
) -> Job | None:
    """Atomically admit a job under the user's allowance on SQLite.

    SQLite serializes writes, so this statement counts after earlier writers
    commit. Revisit locking/isolation if the database backend changes.
    """
    unfinished_count = (
        select(func.count())
        .select_from(Job)
        .join(Upload, Upload.id == Job.upload_id)
        .where(Upload.user_id == user_id, Job.status.in_({"pending", "processing"}))
        .correlate(None)
        .scalar_subquery()
    )
    candidate = select(
        literal(uuid4(), type_=Job.id.type), Upload.id, literal("pending")
    ).where(
        Upload.id == upload_id,
        Upload.user_id == user_id,
        unfinished_count < max_unfinished_jobs,
    )
    statement = (
        insert(Job).from_select(["id", "upload_id", "status"], candidate).returning(Job)
    )
    return session.scalars(statement).one_or_none()
