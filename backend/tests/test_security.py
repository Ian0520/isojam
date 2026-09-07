from app.security import hash_password, verify_password, create_access_token, decode_access_token
from uuid import uuid4
from datetime import timedelta
import pytest
import jwt

def test_hash_password_does_not_store_plaintext():
    password = "correct-horse-battery-staple"
    password_hash = hash_password(password)
    assert isinstance(password_hash, str)
    assert password_hash != password

def test_verify_password_accepts_correct_password():
    password = "correct-horse-battery-staple"
    password_hash = hash_password(password)
    assert verify_password(password, password_hash) 

def test_verify_password_rejects_incorrect_password():
    password = "correct-horse-battery-staple"
    password_hash = hash_password(password)
    assert not verify_password("incorrect-password", password_hash)

def test_hash_password_uses_unique_salts():
    password = "correct-horse-battery-staple"
    password_hash = hash_password(password)
    second_password_hash = hash_password(password)
    assert password_hash != second_password_hash

def test_create_and_decode_access_token():
    user_id = uuid4()
    secret_key = "test-secret-key-that-is-at-least-32-bytes"
    token = create_access_token(
        user_id=user_id,
        secret_key=secret_key,
        expires_delta=timedelta(seconds=30),
    )

    decoded_user_id = decode_access_token(
        token=token,
        secret_key=secret_key,
    )

    assert user_id == decoded_user_id
    
def test_decode_access_token_rejects_expired_token():
    user_id = uuid4()
    secret_key = "test-secret-key-that-is-at-least-32-bytes"
    expired_token = create_access_token(
        user_id=user_id,
        secret_key=secret_key,
        expires_delta=timedelta(seconds=-1),
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired_token, secret_key)

def test_decode_access_token_rejects_token_with_wrong_secret():
    user_id = uuid4()
    secret_key_a = "test-secret-key-a-that-is-at-least-32-bytes"
    secret_key_b = "test-secret-key-b-that-is-at-least-32-bytes"
    token = create_access_token(
        user_id=user_id,
        secret_key=secret_key_a,
        expires_delta=timedelta(seconds=30),
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token, secret_key_b)