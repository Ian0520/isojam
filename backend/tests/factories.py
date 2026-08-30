from app.db_models import Upload, Job, User

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
    upload = Upload(user_id=user.id,
                    original_filename=original_filename,
                    stored_filename=stored_filename,
                    )
    session.add(upload)
    session.flush()
    return upload

def create_test_job(session, upload, status="pending"):
    job = Job(upload_id=upload.id, status="pending")
    session.add(job)
    session.flush()
    return job
