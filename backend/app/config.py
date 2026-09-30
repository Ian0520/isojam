import os
from datetime import timedelta

ACCESS_TOKEN_EXPIRES_DELTA = timedelta(minutes=30)
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_AUDIO_DURATION_SECONDS = 10 * 60


def get_jwt_secret_key() -> str:
    jwt_secret_key = os.environ.get("ISOJAM_JWT_SECRET_KEY")
    if jwt_secret_key is None or not jwt_secret_key.strip():
        raise RuntimeError("ISOJAM_JWT_SECRET_KEY must be set and nonblank")
    return jwt_secret_key


def _get_positive_int(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    try:
        parsed_value = int(value)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a positive integer") from error
    if parsed_value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return parsed_value


def get_max_upload_bytes() -> int:
    return _get_positive_int("ISOJAM_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)


def get_max_audio_duration_seconds() -> int:
    return _get_positive_int(
        "ISOJAM_MAX_AUDIO_DURATION_SECONDS", DEFAULT_MAX_AUDIO_DURATION_SECONDS
    )
