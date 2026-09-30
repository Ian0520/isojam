from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.config import get_audio_storage_dir

AUDIO_STORAGE_DIR = get_audio_storage_dir()
UPLOAD_DIR = AUDIO_STORAGE_DIR / "uploads"
OUTPUT_DIR = AUDIO_STORAGE_DIR / "outputs"


class UploadTooLargeError(ValueError):
    pass


def save_upload(audio_file: UploadFile, *, max_bytes: int) -> Path:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    extension = Path(audio_file.filename).suffix
    new_name = str(uuid4()) + extension
    destination = UPLOAD_DIR / new_name

    output_file = destination.open("xb")
    try:
        with output_file:
            total_bytes = 0
            while chunk := audio_file.file.read(
                min(64 * 1024, max_bytes - total_bytes + 1)
            ):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise UploadTooLargeError("Upload exceeds the maximum allowed size")
                output_file.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def get_upload_path(stored_filename: str) -> Path:
    return UPLOAD_DIR / stored_filename


def get_job_output_dir(job_id: str) -> Path:
    return OUTPUT_DIR / str(job_id)
