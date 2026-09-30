from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db_models import JobOutput


def create_job_output(
    session: Session,
    job_id: UUID,
    stem: str,
    path: str,
) -> JobOutput:
    new_job_output = JobOutput(
        job_id=job_id,
        stem=stem,
        path=path,
    )
    session.add(new_job_output)
    session.flush()
    return new_job_output


def get_job_output(session: Session, job_id: UUID, stem: str) -> JobOutput | None:
    return session.get(JobOutput, (job_id, stem))


def get_job_outputs(session: Session, job_id: UUID) -> Sequence[JobOutput]:
    statement = select(JobOutput).where(JobOutput.job_id == job_id)
    result = session.execute(statement)
    return result.scalars().all()
