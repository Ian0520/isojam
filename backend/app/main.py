from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI

from app.config import ACCESS_TOKEN_EXPIRES_DELTA, get_jwt_secret_key
from app.database import SessionLocal
from app.model import create_model_session
from app.routers.auth import router as auth_router
from app.routers.jobs import router as jobs_router
from app.routers.uploads import router as uploads_router


def create_app(
    model_session_factory=create_model_session,
    db_session_factory=SessionLocal,
    jwt_secret_key: str | None = None,
    access_token_expires_delta: timedelta = ACCESS_TOKEN_EXPIRES_DELTA,
):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.jwt_secret_key = (
            jwt_secret_key if jwt_secret_key is not None else get_jwt_secret_key()
        )
        session = model_session_factory()
        app.state.model_session = session

        try:
            yield
        finally:
            session.close()

    app = FastAPI(lifespan=lifespan)
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
