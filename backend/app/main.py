from fastapi import FastAPI, UploadFile, HTTPException
from app.storage import save_upload
from app.jobs import create_job, get_job

ALLOWED_AUDIO_TYPES = {
    "audio/wav",
    "audio/mpeg",
}


app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/uploads")
def upload_file(audio_file: UploadFile):
    # Check if type is allowed
    if audio_file.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Unsupported audio type",
        )

    saved_path = save_upload(audio_file)
    return {
        "filename": audio_file.filename,
        "content_type": audio_file.content_type,
        "stored_filename": saved_path.name,
    }

@app.post("/jobs")
def create_job_endpoint():
    return create_job()

@app.get("/jobs/{job_id}")
def get_job_endpoint(job_id: str):
    job = get_job(job_id)
    if job is not None:
        return job
    else:
        raise HTTPException(
            status_code=404,
            detail="The requested job does not exist"
        )