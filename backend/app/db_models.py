from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db_types import UTCDateTime


class Base(DeclarativeBase):
    pass


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    original_filename: Mapped[str]
    stored_filename: Mapped[str]


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "execution_backend IN ('local', 'queued')",
            name="ck_jobs_execution_backend",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    upload_id: Mapped[UUID] = mapped_column(ForeignKey("uploads.id"))
    status: Mapped[str]
    execution_backend: Mapped[str] = mapped_column(server_default="local")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_job_number"),
        CheckConstraint("attempt_number > 0", name="ck_job_attempts_positive_number"),
        CheckConstraint(
            "phase IN ('reserved', 'submitting', 'submitted', 'running', "
            "'result_ready', 'uncertain', 'succeeded', 'failed')",
            name="ck_job_attempts_phase",
        ),
        CheckConstraint(
            "(dispatcher_id IS NULL AND dispatcher_generation = 0 "
            "AND reservation_expires_at IS NULL) OR "
            "(dispatcher_id IS NOT NULL AND dispatcher_generation > 0 "
            "AND reservation_expires_at IS NOT NULL)",
            name="ck_job_attempts_dispatcher_authority",
        ),
        UniqueConstraint("invocation_id", name="uq_job_attempts_invocation"),
        CheckConstraint(
            "(execution_authorization_expires_at IS NULL OR "
            "(dispatcher_id IS NOT NULL AND dispatcher_generation > 0)) AND "
            "(invocation_id IS NULL OR "
            "(execution_authorization_expires_at IS NOT NULL AND started_at IS NOT NULL "
            "AND phase IN ('running', 'result_ready', 'uncertain', 'succeeded', 'failed')))",
            name="ck_job_attempts_execution_authority",
        ),
        CheckConstraint(
            "last_heartbeat_at IS NULL OR "
            "(invocation_id IS NOT NULL AND started_at IS NOT NULL "
            "AND last_heartbeat_at >= started_at)",
            name="ck_job_attempts_heartbeat_authority",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    attempt_number: Mapped[int]
    phase: Mapped[str] = mapped_column(server_default="reserved")
    dispatcher_id: Mapped[UUID | None]
    dispatcher_generation: Mapped[int] = mapped_column(server_default="0")
    reservation_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    execution_authorization_expires_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime()
    )
    invocation_id: Mapped[UUID | None]
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class JobSubmissionReceipt(Base):
    __tablename__ = "job_submission_receipts"
    __table_args__ = (
        CheckConstraint(
            "length(key) BETWEEN 1 AND 128", name="ck_job_submission_key_length"
        ),
        CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_job_submission_fingerprint_length",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.current_timestamp()
    )


class JobOutput(Base):
    __tablename__ = "job_outputs"

    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    stem: Mapped[str] = mapped_column(primary_key=True)
    path: Mapped[str]


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(unique=True)
    password_hash: Mapped[str] = mapped_column()
