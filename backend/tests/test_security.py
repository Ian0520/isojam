from app.security import hash_password, verify_password


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