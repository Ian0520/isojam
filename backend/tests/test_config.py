import pytest

from app.config import get_jwt_secret_key, get_max_upload_bytes


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


def test_get_max_upload_bytes_uses_default(monkeypatch):
    monkeypatch.delenv("ISOJAM_MAX_UPLOAD_BYTES", raising=False)
    assert get_max_upload_bytes() == 200 * 1024 * 1024


def test_get_max_upload_bytes_reads_environment(monkeypatch):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", "1024")
    assert get_max_upload_bytes() == 1024


@pytest.mark.parametrize("value", ["0", "-1", "", "abc", "1.5"])
def test_get_max_upload_bytes_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_MAX_UPLOAD_BYTES", value)
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_UPLOAD_BYTES"):
        get_max_upload_bytes()
