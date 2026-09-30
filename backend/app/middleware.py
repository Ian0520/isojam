from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_API_REQUEST_BYTES = 16 * 1024
UPLOAD_REQUEST_OVERHEAD_BYTES = 64 * 1024


class RequestSizeLimitMiddleware:
    """Choose a body limit before request data reaches FastAPI's parsers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        root_path = scope.get("root_path", "").rstrip("/")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]

        max_body_size = MAX_API_REQUEST_BYTES
        if scope["method"] == "POST" and path in {"/uploads", "/uploads/"}:
            max_body_size = (
                scope["app"].state.max_upload_bytes + UPLOAD_REQUEST_OVERHEAD_BYTES
            )

        limiter = RequestBodyLimitMiddleware(self.app, max_body_size=max_body_size)
        await limiter(scope, receive, send)
