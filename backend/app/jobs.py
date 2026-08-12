from uuid import uuid4

jobs = {}

ALLOWED_STATUSES = {"pending",
                    "processing", 
                    "completed", 
                    "failed"}

def create_job(upload_id: str):
    job_id = str(uuid4())

    new_job = {
        "id" : job_id,
        "status" : "pending",
        "upload_id": upload_id
    }

    jobs[job_id] = new_job

    return new_job

def get_job(job_id):
    return jobs.get(job_id)

def update_job_status(job_id: str, status: str):
    job = get_job(job_id)
    if job is None:
        return None
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid job status: {status}")
    job["status"] = status
    return job