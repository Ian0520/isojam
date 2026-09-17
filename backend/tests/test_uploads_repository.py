from app.db_models import Upload
import app.repositories.uploads as uploads
from uuid import UUID, uuid4

from tests.factories import create_test_user

def test_create_upload_persists_after_commit(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        upload = uploads.create_upload(session, 
                              user_id=user_id,
                              original_filename="song.wav",
                              stored_filename="some-uuid.wav")
        assert upload.id is not None
        upload_id = upload.id
        session.commit()

    with test_session_factory() as session:
        result = session.get(Upload, upload_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.user_id == user_id
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"


def test_get_upload_returns_existing_upload(test_session_factory):
    with test_session_factory() as session:
        user = create_test_user(session)
        user_id = user.id
        upload = uploads.create_upload(session, 
                        user_id=user_id,
                        original_filename="song.wav",
                        stored_filename="some-uuid.wav")
        assert upload.id is not None
        upload_id = upload.id
        session.commit()

    with test_session_factory() as session:
        result = uploads.get_upload(session, upload_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.user_id == user_id
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"

def test_get_upload_returns_none_for_missing_upload(test_session_factory):
    with test_session_factory() as session:
        result = uploads.get_upload(session, uuid4())
        assert result is None