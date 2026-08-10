from uuid import uuid4

jobs = {}

def create_job():
    job_id = str(uuid4())

    new_job = {
        "id" : job_id,
        "status" : "pending",
    }

    jobs[job_id] = new_job

    return new_job

def get_job(job_id):
    return jobs.get(job_id)