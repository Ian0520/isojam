from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.audio import AudioTooLongError, InvalidAudioError, validate_wav
from app.audio_conversion import (
    ConversionBusyError,
    ConversionUnavailableError,
    ConvertedAudioTooLargeError,
    convert_to_wav,
    supports_audio_upload,
)
from app.auth import get_current_user
from app.database import get_db
from app.db_models import User
from app.repositories import uploads
from app.schemas import UploadResponse
from app.storage import UploadTooLargeError, save_upload

router = APIRouter()


@router.post("/uploads", response_model=UploadResponse)
def upload_file(
    audio_file: UploadFile,
    request: Request,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> UploadResponse:
    if not supports_audio_upload(audio_file.filename, audio_file.content_type):
        raise HTTPException(
            status_code=415,
            detail="Unsupported audio type",
        )

    # Authentication used this session to read the user. Release that read
    # transaction before potentially slow file I/O and decoding.
    user_id = current_user.id
    session.rollback()

    try:
        saved_path = save_upload(
            audio_file, max_bytes=request.app.state.max_upload_bytes
        )
    except UploadTooLargeError as error:
        raise HTTPException(
            status_code=413,
            detail="Upload exceeds the maximum allowed size",
        ) from error

    source_path = saved_path
    try:
        if source_path.suffix.lower() != ".wav":
            saved_path = convert_to_wav(
                source_path,
                max_duration_seconds=request.app.state.max_audio_duration_seconds,
                max_bytes=request.app.state.max_converted_audio_bytes,
            )
        validate_wav(
            saved_path,
            max_duration_seconds=request.app.state.max_audio_duration_seconds,
        )
        if source_path != saved_path:
            source_path.unlink()
    except (
        InvalidAudioError,
        AudioTooLongError,
        ConvertedAudioTooLargeError,
        ConversionBusyError,
        ConversionUnavailableError,
    ) as error:
        saved_path.unlink(missing_ok=True)
        source_path.unlink(missing_ok=True)
        if isinstance(error, (AudioTooLongError, ConvertedAudioTooLargeError)):
            status_code = 413
        elif isinstance(error, (ConversionBusyError, ConversionUnavailableError)):
            status_code = 503
        else:
            status_code = 422
        raise HTTPException(
            status_code=status_code,
            detail=str(error),
        ) from error
    except BaseException:
        saved_path.unlink(missing_ok=True)
        source_path.unlink(missing_ok=True)
        raise

    try:
        upload_record = uploads.create_upload(
            session=session,
            original_filename=audio_file.filename,
            stored_filename=saved_path.name,
            user_id=user_id,
        )
        upload_id = upload_record.id
        session.commit()
    except BaseException:
        try:
            session.rollback()
        finally:
            saved_path.unlink(missing_ok=True)
        raise

    return UploadResponse(
        id=upload_id,
        filename=audio_file.filename,
        content_type=audio_file.content_type,
    )
