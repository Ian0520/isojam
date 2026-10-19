from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.audio_conversion import get_audio_upload_types

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
router = APIRouter()


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        # HTML and its scripts/styles must agree after a container replacement.
        # Keep ETag/304 support, but require validation before reusing an asset.
        response.headers["Cache-Control"] = "no-cache"
        return response


@router.get("/", include_in_schema=False)
def home():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'"
            ),
        },
    )


@router.get("/ui-info", include_in_schema=False)
def ui_info(request: Request):
    return {
        "processing_mode": request.app.state.processing_mode,
        "registration_mode": request.app.state.registration_mode,
        "max_upload_bytes": request.app.state.max_upload_bytes,
        "max_audio_duration_seconds": request.app.state.max_audio_duration_seconds,
        "max_converted_audio_bytes": request.app.state.max_converted_audio_bytes,
        "audio_upload_types": get_audio_upload_types(),
    }
