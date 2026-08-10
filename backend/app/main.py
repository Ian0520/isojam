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
    saved_path = save_upload(audio_file)
    return {
        "filename": audio_file.filename,
        "content_type": audio_file.content_type,
        "stored_filename": saved_path.name,
    }