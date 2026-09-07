from pwdlib import PasswordHash
from uuid import UUID
from datetime import datetime, timezone, timedelta
import jwt

_password_hasher = PasswordHash.recommended()

def hash_password(password: str) -> str:
    password_hash = _password_hasher.hash(password)
    return password_hash

def verify_password(password: str, password_hash: str) -> bool:
    return _password_hasher.verify(password, password_hash)

def create_access_token(
    user_id: UUID,
    secret_key: str,
    expires_delta: timedelta,
) -> str:
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + expires_delta
    }
    return jwt.encode(
        payload,
        secret_key,
        algorithm="HS256",
        )
    
    
def decode_access_token(
    token: str,
    secret_key: str,
) -> UUID:
    payload = jwt.decode(
        token,
        key=secret_key,
        algorithms=["HS256"],
        )
        
    return UUID(payload["sub"])

    