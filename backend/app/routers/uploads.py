from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.db_models import User
from app.repositories import uploads
from app.schemas import UploadResponse
from app.storage import UploadTooLargeError, save_upload

router = APIRouter()

ALLOWED_AUDIO_TYPES = {
    "audio/wav",
}


@router.post("/uploads", response_model=UploadResponse)
def upload_file(
    audio_file: UploadFile,
    request: Request,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    # Check if type is allowed
    if (
        audio_file.content_type not in ALLOWED_AUDIO_TYPES
        or Path(audio_file.filename or "").suffix.lower() != ".wav"
    ):
        raise HTTPException(
            status_code=415,
            detail="Unsupported audio type",
        )

    try:
        saved_path = save_upload(
            audio_file, max_bytes=request.app.state.max_upload_bytes
        )
    except UploadTooLargeError as error:
        raise HTTPException(
            status_code=413,
            detail="Upload exceeds the maximum allowed size",
        ) from error

    upload_record = uploads.create_upload(
        session=session,
        original_filename=audio_file.filename,
        stored_filename=saved_path.name,
        user_id=current_user.id,
    )
    upload_id = upload_record.id
    session.commit()

    return UploadResponse(
        id=upload_id,
        filename=audio_file.filename,
        content_type=audio_file.content_type,
    )
