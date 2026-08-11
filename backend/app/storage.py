from fastapi import UploadFile
from pathlib import Path
from uuid import uuid4
import shutil

PROJECT_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
OUTPUT_DIR = PROJECT_ROOT / "data" / "outtputs"

def save_upload(audio_file: UploadFile) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        
    extension = Path(audio_file.filename).suffix
    new_name = str(uuid4()) + extension
    destination = UPLOAD_DIR / new_name

    with destination.open("wb") as output_file:
            shutil.copyfileobj(audio_file.file, output_file)
    return destination

def get_upload_path(stored_filename: str)-> Path:
    return UPLOAD_DIR / stored_filename

def get_job_output_dir(job_id: str) -> Path:
    return OUTPUT_DIR / job_id