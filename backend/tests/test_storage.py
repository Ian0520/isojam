from io import BytesIO

import pytest
from fastapi import UploadFile

import app.storage as storage


def test_get_job_output_dir_returns_job_specific_path(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "OUTPUT_DIR", tmp_path)
    job_id = "job-123"
    output_dir = storage.get_job_output_dir(job_id)
    assert output_dir == tmp_path / job_id


@pytest.mark.parametrize("payload", [b"123", b"1234"])
def test_save_upload_accepts_files_up_to_byte_limit(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    audio_file = UploadFile(file=BytesIO(payload), filename="test.wav")
    saved_path = storage.save_upload(audio_file, max_bytes=4)
    assert saved_path.read_bytes() == payload
    assert saved_path.parent == tmp_path


@pytest.mark.parametrize("declared_size", [None, 1])
def test_save_upload_counts_actual_bytes_and_removes_partial_file(
    tmp_path, monkeypatch, declared_size
):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

    class BoundedStream(BytesIO):
        def read(self, size=-1):
            assert size > 0, "Upload copying must use bounded reads"
            return super().read(size)

    max_bytes = 64 * 1024
    stream = BoundedStream(b"a" * (max_bytes + 100))
    audio_file = UploadFile(file=stream, filename="test.wav", size=declared_size)
    with pytest.raises(storage.UploadTooLargeError):
        storage.save_upload(audio_file, max_bytes=max_bytes)
    assert stream.tell() == max_bytes + 1
    assert list(tmp_path.iterdir()) == []


def test_save_upload_removes_partial_file_when_read_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)

    class FailingStream(BytesIO):
        def read(self, size=-1):
            if self.tell() > 0:
                raise OSError("Upload read failed")
            return super().read(min(size, 4))

    audio_file = UploadFile(file=FailingStream(b"abcdefgh"), filename="test.wav")
    with pytest.raises(OSError, match="Upload read failed"):
        storage.save_upload(audio_file, max_bytes=100)
    assert list(tmp_path.iterdir()) == []


def test_save_upload_preserves_existing_file_on_filename_collision(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(storage, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(storage, "uuid4", lambda: "existing")
    existing = tmp_path / "existing.wav"
    existing.write_bytes(b"original")
    audio_file = UploadFile(file=BytesIO(b"replacement"), filename="test.wav")
    with pytest.raises(FileExistsError):
        storage.save_upload(audio_file, max_bytes=100)
    assert existing.read_bytes() == b"original"
