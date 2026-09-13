import os
from datetime import timedelta

ACCESS_TOKEN_EXPIRES_DELTA = timedelta(minutes=30)

def get_jwt_secret_key() -> str:
    jwt_secret_key = os.environ.get("ISOJAM_JWT_SECRET_KEY")
    if jwt_secret_key is None or not jwt_secret_key.strip():
        raise RuntimeError("ISOJAM_JWT_SECRET_KEY must be set and nonblank")
    return jwt_secret_key