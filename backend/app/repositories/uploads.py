from uuid import UUID

from sqlalchemy.orm import Session

from app.db_models import Upload


def create_upload(
    session: Session,
    original_filename: str,
    stored_filename: str,
    user_id: UUID,
) -> Upload:
    upload = Upload(
        user_id=user_id,
        original_filename=original_filename,
        stored_filename=stored_filename,
    )
    session.add(upload)
    session.flush()
    return upload


def get_upload(session: Session, upload_id: UUID) -> Upload | None:
    return session.get(Upload, upload_id)
