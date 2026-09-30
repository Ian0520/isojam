from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app.config import (
    ACCESS_TOKEN_EXPIRES_DELTA,
    get_jwt_secret_key,
    get_max_audio_duration_seconds,
    get_max_upload_bytes,
)
from app.database import SessionLocal
from app.middleware import RequestSizeLimitMiddleware
from app.model import create_model_session
from app.routers.auth import router as auth_router
from app.routers.jobs import router as jobs_router
from app.routers.uploads import router as uploads_router


def create_app(
    model_session_factory=create_model_session,
    db_session_factory=SessionLocal,
    jwt_secret_key: str | None = None,
    access_token_expires_delta: timedelta = ACCESS_TOKEN_EXPIRES_DELTA,
    max_upload_bytes: int | None = None,
    max_audio_duration_seconds: int | None = None,
):
    if max_upload_bytes is not None and (
        type(max_upload_bytes) is not int or max_upload_bytes <= 0
    ):
        raise ValueError("max_upload_bytes must be a positive integer")
    if max_audio_duration_seconds is not None and (
        type(max_audio_duration_seconds) is not int or max_audio_duration_seconds <= 0
    ):
        raise ValueError("max_audio_duration_seconds must be a positive integer")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jwt_secret_key = (
            jwt_secret_key if jwt_secret_key is not None else get_jwt_secret_key()
        )
        app.state.max_upload_bytes = (
            max_upload_bytes if max_upload_bytes is not None else get_max_upload_bytes()
        )
        app.state.max_audio_duration_seconds = (
            max_audio_duration_seconds
            if max_audio_duration_seconds is not None
            else get_max_audio_duration_seconds()
        )
        session = model_session_factory()
        app.state.model_session = session

        try:
            yield
        finally:
            session.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RequestSizeLimitMiddleware)
    app.state.db_session_factory = db_session_factory
    app.state.access_token_expires_delta = access_token_expires_delta

    @app.get("/health")
    def health():
        return {"status": "ok"}

    app.include_router(uploads_router)

    app.include_router(jobs_router)
    app.include_router(auth_router)

    return app


app = create_app()
