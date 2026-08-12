import app.jobs as jobs
import app.uploads as uploads
import app.storage as storage
import app.separation as separation

def process_job(job_id: str, session):
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

    output_dir = storage.get_job_output_dir(job_id)
    try:
        paths = separation.separate_audio(input_path, output_dir, session)
    except Exception:
        jobs.update_job_status(job_id, "failed")
        raise
    paths = {
        stem: str(path)
        for stem, path in paths.items()
    }
    job["outputs"] = paths

    job = jobs.update_job_status(job_id, "completed")
    
    return job