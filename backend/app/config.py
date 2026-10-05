import os
from datetime import timedelta
from pathlib import Path

ACCESS_TOKEN_EXPIRES_DELTA = timedelta(minutes=30)
DEFAULT_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_AUDIO_DURATION_SECONDS = 10 * 60
DEFAULT_MAX_UNFINISHED_JOBS_PER_USER = 2
PROCESSING_MODES = ("local", "disabled", "queued")


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


def get_max_unfinished_jobs_per_user() -> int:
    return _get_positive_int(
        "ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", DEFAULT_MAX_UNFINISHED_JOBS_PER_USER
    )


def get_audio_storage_dir() -> Path:
    value = os.environ.get("ISOJAM_AUDIO_STORAGE_DIR")
    if value is None:
        directory = Path(__file__).resolve().parents[2] / "data"
    else:
        if not value.strip() or not Path(value).is_absolute():
            raise RuntimeError(
                "ISOJAM_AUDIO_STORAGE_DIR must be a nonblank absolute path"
            )
        directory = Path(value).resolve()
    if directory.exists() and not directory.is_dir():
        raise RuntimeError("ISOJAM_AUDIO_STORAGE_DIR must refer to a directory")
    return directory


def get_database_path() -> Path:
    value = os.environ.get("ISOJAM_DATABASE_PATH")
    if value is None:
        path = Path(__file__).resolve().parents[2] / "data" / "isojam.db"
    else:
        if not value.strip() or not Path(value).is_absolute():
            raise RuntimeError(
                "ISOJAM_DATABASE_PATH must be a nonblank absolute file path"
            )
        path = Path(value).resolve()
    if path.exists() and not path.is_file():
        raise RuntimeError("ISOJAM_DATABASE_PATH must refer to a file")
    if path.parent.exists() and not path.parent.is_dir():
        raise RuntimeError("ISOJAM_DATABASE_PATH parent must be a directory")
    return path


def get_processing_mode() -> str:
    mode = os.environ.get("ISOJAM_PROCESSING_MODE", "local")
    if mode not in PROCESSING_MODES:
        raise RuntimeError(
            "ISOJAM_PROCESSING_MODE must be 'local', 'disabled', or 'queued'"
        )
    return mode
