from app.db_models import Job
from uuid import UUID

ALLOWED_STATUSES = {"pending",
                    "processing", 
                    "completed", 
                    "failed"}

def create_job(session, upload_id: UUID):
    new_job = Job(upload_id=upload_id,
                   status="pending"
                   )

    session.add(new_job)
    session.flush()
    return new_job

def get_job(session, job_id):
    return session.get(Job, job_id)

def update_job_status(session, job_id: UUID, status: str):
    job = get_job(session, job_id)
    if job is None:
        return None
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid job status: {status}")
    job.status = status
    session.flush()
    return job
