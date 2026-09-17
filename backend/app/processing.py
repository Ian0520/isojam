import app.repositories.jobs as jobs
import app.repositories.uploads as uploads
import app.storage as storage
import app.separation as separation
import app.repositories.job_outputs as job_outputs
from uuid import UUID

def process_job(job_id: UUID, model_session, db_session_factory):
    with db_session_factory() as db:
        job = jobs.get_job(db, job_id)
        if job is None:
            raise KeyError("job not found") 
        upload_id = job.upload_id
        upload = uploads.get_upload(db, upload_id)
        if upload is None:
            jobs.update_job_status(db, job_id, "failed")
            db.commit()
            raise RuntimeError("upload not found")
        stored_filename = upload.stored_filename

    input_path = storage.get_upload_path(stored_filename)
    if not input_path.exists():
        with db_session_factory() as db:
            jobs.update_job_status(db, job_id, "failed")
            db.commit()
            raise FileNotFoundError("file not found")

    with db_session_factory() as db:
        job = jobs.update_job_status(db, job_id, "processing")
        db.commit()
    output_dir = storage.get_job_output_dir(job_id)
    try:
        paths = separation.separate_audio(input_path, output_dir, model_session)
    except Exception:
        with db_session_factory() as db:
            jobs.update_job_status(db, job_id, "failed")
            db.commit()
            raise

    with db_session_factory() as db:
        for stem, path in paths.items():
            job_outputs.create_job_output(db, job_id, stem, str(path))
        job = jobs.update_job_status(db, job_id, "completed")
        db.commit()
    