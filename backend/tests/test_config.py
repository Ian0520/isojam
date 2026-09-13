from app.config import get_jwt_secret_key
import pytest

def test_get_jwt_secret_key_reads_environment(monkeypatch):
    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", "test-secret-key")
    jwt_secret_key = get_jwt_secret_key()
    assert jwt_secret_key == "test-secret-key"

def test_get_jwt_secret_key_rejects_missing_value(monkeypatch):
    monkeypatch.delenv("ISOJAM_JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_jwt_secret_key()

@pytest.mark.parametrize("blank_secret_key", ["", " "])
def test_get_jwt_secret_key_rejects_blank_value(monkeypatch, blank_secret_key):
    monkeypatch.setenv("ISOJAM_JWT_SECRET_KEY", blank_secret_key)
    with pytest.raises(RuntimeError):
        get_jwt_secret_key()