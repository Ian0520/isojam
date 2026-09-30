import os
from datetime import timedelta

ACCESS_TOKEN_EXPIRES_DELTA = timedelta(minutes=30)
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024


def get_jwt_secret_key() -> str:
    jwt_secret_key = os.environ.get("ISOJAM_JWT_SECRET_KEY")
    if jwt_secret_key is None or not jwt_secret_key.strip():
        raise RuntimeError("ISOJAM_JWT_SECRET_KEY must be set and nonblank")
    return jwt_secret_key


def get_max_upload_bytes() -> int:
    value = os.environ.get("ISOJAM_MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
    try:
        max_upload_bytes = int(value)
    except ValueError as error:
        raise RuntimeError(
            "ISOJAM_MAX_UPLOAD_BYTES must be a positive integer"
        ) from error
    if max_upload_bytes <= 0:
        raise RuntimeError("ISOJAM_MAX_UPLOAD_BYTES must be a positive integer")
    return max_upload_bytes
