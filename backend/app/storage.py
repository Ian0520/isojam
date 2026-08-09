from fastapi import UploadFile
from pathlib import Path
from uuid import uuid4
import shutil

UPLOAD_DIR = Path("uploads")

def save_upload(audio_file: UploadFile) -> Path:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        
        extension = Path(audio_file.filename).suffix
        new_name = str(uuid4()) + extension
        destination = UPLOAD_DIR / new_name

        with destination.open("wb") as output_file:
            shutil.copyfileobj(audio_file.file, output_file)
        return destination