import wave
from io import BytesIO

from app.db_models import Job, Upload, User


def create_test_user(session, email="user@example.com"):
    user = User(
        email=email,
        password_hash="some-hash",
    )
    session.add(user)
    session.flush()
    return user


def create_test_upload(
    session,
    user,
    original_filename="song.wav",
    stored_filename="some-uuid.wav",
):
    upload = Upload(
        user_id=user.id,
        original_filename=original_filename,
        stored_filename=stored_filename,
    )
    session.add(upload)
    session.flush()
    return upload


def create_test_job(session, upload, status="pending"):
    job = Job(upload_id=upload.id, status=status)
    session.add(job)
    session.flush()
    return job


def make_wav_bytes(
    *, frames: int = 16, sample_rate: int = 44100, channels: int = 2
) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frames * channels)
    return buffer.getvalue()
