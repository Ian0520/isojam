from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
router = APIRouter()


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
        "max_upload_bytes": request.app.state.max_upload_bytes,
        "max_audio_duration_seconds": request.app.state.max_audio_duration_seconds,
    }
