"""Durable, bounded import admission for the single-host SQLite stack."""

from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.orm import Session

from app.db_models import AudioImport

UNFINISHED = ("pending", "processing")


class ImportConflictError(ValueError):
    pass


def replay(
    session: Session, user_id: UUID, key: str, video_id: str
) -> AudioImport | None:
    item = session.scalar(
        select(AudioImport).where(
            AudioImport.user_id == user_id, AudioImport.submission_key == key
        )
    )
    if item is not None and item.video_id != video_id:
        raise ImportConflictError("Idempotency-Key was already used for another video")
    return item


def admit(
    session: Session,
    user_id: UUID,
    key: str,
    video_id: str,
    *,
    per_user_limit: int,
    global_limit: int,
) -> AudioImport | None:
    """Count and insert in one write statement, also excluding a committed key.

    SQLite serializes writers. Holding this transaction until commit makes
    simultaneous admissions honor both limits and converge on the same key.
    """
    active = (
        select(func.count())
        .select_from(AudioImport)
        .where(AudioImport.status.in_(UNFINISHED))
    )
    same_key = (
        select(AudioImport.id)
        .where(AudioImport.user_id == user_id, AudioImport.submission_key == key)
        .exists()
    )
    candidate = select(
        literal(uuid4(), type_=AudioImport.id.type),
        literal(user_id, type_=AudioImport.user_id.type),
        literal(key),
        literal(video_id),
        literal("pending"),
    ).where(
        active.correlate(None).scalar_subquery() < global_limit,
        active.where(AudioImport.user_id == user_id).correlate(None).scalar_subquery()
        < per_user_limit,
        ~same_key,
    )
    item = session.scalar(
        insert(AudioImport)
        .from_select(
            ["id", "user_id", "submission_key", "video_id", "status"], candidate
        )
        .returning(AudioImport)
    )
    return item if item is not None else replay(session, user_id, key, video_id)


def claim_next(session: Session) -> AudioImport | None:
    oldest = (
        select(AudioImport.id)
        .where(AudioImport.status == "pending")
        .order_by(AudioImport.created_at, AudioImport.id)
        .limit(1)
        .scalar_subquery()
    )
    return session.scalar(
        update(AudioImport)
        .where(AudioImport.id == oldest, AudioImport.status == "pending")
        .values(status="processing")
        .returning(AudioImport)
    )
