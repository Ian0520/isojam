import app.jobs as jobs
import app.uploads as uploads
import app.storage as storage


def process_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise KeyError("job not found") 
    
    upload_id = job["upload_id"]
    upload = uploads.get_upload(upload_id)
    if upload is None:
        jobs.update_job_status(job_id, "failed")
        raise RuntimeError("upload not found")
    
    stored_filename = upload["stored_filename"]
    input_path = storage.get_upload_path(stored_filename)
    if not input_path.exists():
        jobs.update_job_status(job_id, "failed")
        raise FileNotFoundError("file not found")
    
    job = jobs.update_job_status(job_id, "processing")
    return job