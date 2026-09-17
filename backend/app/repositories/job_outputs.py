from app.db_models import JobOutput
from uuid import UUID
from sqlalchemy import select

def create_job_output(session, job_id: UUID, stem: str, path: str):
    new_job_output = JobOutput(job_id=job_id,
                               stem=stem,
                               path=path,)
    session.add(new_job_output)
    session.flush()
    return new_job_output

def get_job_output(session, job_id: UUID, stem: str):
    return session.get(JobOutput, (job_id, stem))

def get_job_outputs(session, job_id: UUID):
    statement = select(JobOutput).where(JobOutput.job_id==job_id)
    result = session.execute(statement)
    return result.scalars().all()