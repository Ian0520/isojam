from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import commit_session, get_db
from app.db_models import AudioImport, User
from app.repositories import audio_imports
from app.schemas import AudioImportResponse, CreateAudioImportRequest
from app.youtube import youtube_video_id

router = APIRouter()


def serialize_import(item: AudioImport) -> AudioImportResponse:
    return AudioImportResponse.model_validate(item, from_attributes=True)


@router.post("/imports", response_model=AudioImportResponse, status_code=202)
def create_import(
    body: CreateAudioImportRequest,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=128,
            pattern=r"^[A-Za-z0-9._:-]+$",
        ),
    ],
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AudioImportResponse:
    try:
        video_id = youtube_video_id(body.url)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    try:
        item = audio_imports.replay(session, current_user.id, idempotency_key, video_id)
        if item is not None:
            return serialize_import(item)
        if not request.app.state.youtube_imports_enabled:
            raise HTTPException(
                status_code=503, detail="YouTube imports are unavailable"
            )
        item = audio_imports.admit(
            session,
            current_user.id,
            idempotency_key,
            video_id,
            per_user_limit=request.app.state.max_unfinished_imports_per_user,
            global_limit=request.app.state.max_unfinished_imports,
        )
        if item is None:
            raise HTTPException(
                status_code=429, detail="The audio import queue is full"
            )
        commit_session(session)
    except audio_imports.ImportConflictError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_import(item)


@router.get("/imports", response_model=list[AudioImportResponse])
def list_imports(
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AudioImportResponse]:
    items = session.scalars(
        select(AudioImport)
        .where(AudioImport.user_id == current_user.id)
        .order_by(AudioImport.created_at.desc(), AudioImport.id.desc())
        .limit(limit)
    )
    return [serialize_import(item) for item in items]


@router.get("/imports/{import_id}", response_model=AudioImportResponse)
def get_import(
    import_id: UUID,
    session: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AudioImportResponse:
    item = session.scalar(
        select(AudioImport).where(
            AudioImport.id == import_id, AudioImport.user_id == current_user.id
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="The audio import does not exist")
    return serialize_import(item)
