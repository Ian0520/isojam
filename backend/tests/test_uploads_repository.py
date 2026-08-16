from app.db_models import Upload
import app.uploads as uploads
from uuid import UUID, uuid4


def test_create_upload_persists_after_commit(tmp_path, test_session_factory):
    with test_session_factory() as session:
        upload = uploads.create_upload(session, 
                              original_filename="song.wav",
                              stored_filename="some-uuid.wav")
        assert upload.id is not None
        upload_id = upload.id
        session.commit()

    with test_session_factory() as session:
        result = session.get(Upload, upload_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"


def test_get_upload_returns_existing_upload(tmp_path, test_session_factory):
    with test_session_factory() as session:
        upload = uploads.create_upload(session, 
                        original_filename="song.wav",
                        stored_filename="some-uuid.wav")
        assert upload.id is not None
        upload_id = upload.id
        session.commit()

    with test_session_factory() as session:
        result = uploads.get_upload(session, upload_id)
        assert result is not None
        assert isinstance(result.id, UUID)
        assert result.original_filename == "song.wav"
        assert result.stored_filename == "some-uuid.wav"

def test_get_upload_returns_none_for_missing_upload(tmp_path, test_session_factory):
    with test_session_factory() as session:
        result = uploads.get_upload(session, uuid4())
        assert result is None