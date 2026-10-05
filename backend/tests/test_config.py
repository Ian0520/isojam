from pathlib import Path

import pytest

import app.config as config
from app.config import (
    get_audio_storage_dir,
    get_database_path,
    get_jwt_secret_key,
    get_max_audio_duration_seconds,
    get_max_unfinished_jobs_per_user,
    get_max_upload_bytes,
    get_processing_mode,
)


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


def test_get_max_audio_duration_seconds_uses_default(monkeypatch):
    monkeypatch.delenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", raising=False)
    assert get_max_audio_duration_seconds() == 600


def test_get_max_audio_duration_seconds_reads_environment(monkeypatch):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", "300")
    assert get_max_audio_duration_seconds() == 300


@pytest.mark.parametrize("value", ["0", "-1", "", "abc", "1.5"])
def test_get_max_audio_duration_seconds_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_MAX_AUDIO_DURATION_SECONDS", value)
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_AUDIO_DURATION_SECONDS"):
        get_max_audio_duration_seconds()


def test_get_max_unfinished_jobs_per_user_uses_default(monkeypatch):
    monkeypatch.delenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", raising=False)
    assert get_max_unfinished_jobs_per_user() == 2


def test_get_max_unfinished_jobs_per_user_reads_environment(monkeypatch):
    monkeypatch.setenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", "3")
    assert get_max_unfinished_jobs_per_user() == 3


@pytest.mark.parametrize("value", ["0", "-1", "", "abc", "1.5"])
def test_get_max_unfinished_jobs_per_user_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_MAX_UNFINISHED_JOBS_PER_USER", value)
    with pytest.raises(RuntimeError, match="ISOJAM_MAX_UNFINISHED_JOBS_PER_USER"):
        get_max_unfinished_jobs_per_user()


def test_get_audio_storage_dir_defaults_to_project_data_independent_of_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ISOJAM_AUDIO_STORAGE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert (
        get_audio_storage_dir() == Path(config.__file__).resolve().parents[2] / "data"
    )


def test_get_audio_storage_dir_reads_absolute_path_without_creating_it(
    tmp_path, monkeypatch
):
    audio_dir = tmp_path / "audio storage"
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(audio_dir))
    assert get_audio_storage_dir() == audio_dir
    assert not audio_dir.exists()


@pytest.mark.parametrize("value", ["", " ", "data", "../data", "~/data"])
def test_get_audio_storage_dir_rejects_blank_or_relative_paths(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", value)
    with pytest.raises(RuntimeError, match="ISOJAM_AUDIO_STORAGE_DIR"):
        get_audio_storage_dir()


def test_get_audio_storage_dir_rejects_existing_file(tmp_path, monkeypatch):
    existing_file = tmp_path / "audio"
    existing_file.write_bytes(b"existing file")
    monkeypatch.setenv("ISOJAM_AUDIO_STORAGE_DIR", str(existing_file))
    with pytest.raises(RuntimeError, match="ISOJAM_AUDIO_STORAGE_DIR.*directory"):
        get_audio_storage_dir()
    assert existing_file.read_bytes() == b"existing file"


def test_get_database_path_defaults_to_project_database_independent_of_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("ISOJAM_DATABASE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert (
        get_database_path()
        == Path(config.__file__).resolve().parents[2] / "data" / "isojam.db"
    )


def test_get_database_path_reads_absolute_path_without_creating_files(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "metadata storage" / "isojam.db"
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(database_path))
    assert get_database_path() == database_path
    assert not database_path.parent.exists()


@pytest.mark.parametrize(
    "value", ["", " ", "data/isojam.db", "../isojam.db", "~/isojam.db"]
)
def test_get_database_path_rejects_blank_or_relative_paths(monkeypatch, value):
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", value)
    with pytest.raises(RuntimeError, match="ISOJAM_DATABASE_PATH"):
        get_database_path()


def test_get_database_path_rejects_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="ISOJAM_DATABASE_PATH.*file"):
        get_database_path()


def test_get_database_path_preserves_existing_file(tmp_path, monkeypatch):
    database_path = tmp_path / "existing.db"
    database_path.write_bytes(b"existing database")
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(database_path))
    assert get_database_path() == database_path
    assert database_path.read_bytes() == b"existing database"


def test_get_database_path_rejects_file_as_parent(tmp_path, monkeypatch):
    parent = tmp_path / "metadata"
    parent.write_bytes(b"existing file")
    monkeypatch.setenv("ISOJAM_DATABASE_PATH", str(parent / "isojam.db"))
    with pytest.raises(RuntimeError, match="ISOJAM_DATABASE_PATH.*parent"):
        get_database_path()
    assert parent.read_bytes() == b"existing file"


def test_get_processing_mode_defaults_to_local(monkeypatch):
    monkeypatch.delenv("ISOJAM_PROCESSING_MODE", raising=False)
    assert get_processing_mode() == "local"


@pytest.mark.parametrize("mode", ["local", "disabled"])
def test_get_processing_mode_reads_environment(monkeypatch, mode):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", mode)
    assert get_processing_mode() == mode


@pytest.mark.parametrize("mode", ["", " ", "LOCAL", "worker"])
def test_get_processing_mode_rejects_invalid_values(monkeypatch, mode):
    monkeypatch.setenv("ISOJAM_PROCESSING_MODE", mode)
    with pytest.raises(RuntimeError, match="ISOJAM_PROCESSING_MODE"):
        get_processing_mode()
