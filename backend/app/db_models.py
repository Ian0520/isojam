from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
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


class AudioImport(Base):
    __tablename__ = "audio_imports"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "submission_key", name="uq_audio_imports_submission"
        ),
        CheckConstraint(
            "length(video_id) = 11 AND video_id NOT GLOB '*[^A-Za-z0-9_-]*'",
            name="ck_audio_imports_video_id",
        ),
        CheckConstraint(
            "length(submission_key) BETWEEN 1 AND 128",
            name="ck_audio_imports_submission_key",
        ),
        CheckConstraint(
            "(status IN ('pending', 'processing') AND upload_id IS NULL "
            "AND error_code IS NULL) OR "
            "(status = 'ready' AND upload_id IS NOT NULL AND error_code IS NULL) OR "
            "(status = 'failed' AND upload_id IS NULL AND error_code IS NOT NULL)",
            name="ck_audio_imports_result",
        ),
        Index("ix_audio_imports_queue", "status", "created_at", "id"),
        Index("ix_audio_imports_owner", "user_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[str] = mapped_column(String(11))
    submission_key: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(server_default="pending")
    upload_id: Mapped[UUID | None] = mapped_column(ForeignKey("uploads.id"))
    filename: Mapped[str | None]
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), server_default=func.current_timestamp()
    )


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
        CheckConstraint(
            "(execution_stopped_at IS NULL AND execution_exit_code IS NULL) OR "
            "(execution_stopped_at IS NOT NULL AND execution_exit_code IS NOT NULL "
            "AND typeof(execution_exit_code) = 'integer' "
            "AND execution_exit_code BETWEEN -255 AND 255 "
            "AND invocation_id IS NOT NULL AND started_at IS NOT NULL "
            "AND execution_stopped_at >= started_at "
            "AND (last_heartbeat_at IS NULL OR execution_stopped_at >= last_heartbeat_at) "
            "AND (finished_at IS NULL OR finished_at >= execution_stopped_at) "
            "AND phase IN ('running', 'result_ready', 'uncertain', 'succeeded', 'failed'))",
            name="ck_job_attempts_execution_stopped",
        ),
        CheckConstraint(
            "(local_worker_pid IS NULL AND local_worker_id IS NULL) OR "
            "(local_worker_pid IS NOT NULL AND local_worker_id IS NOT NULL "
            "AND typeof(local_worker_pid) = 'integer' AND local_worker_pid > 0 "
            "AND invocation_id IS NOT NULL AND started_at IS NOT NULL)",
            name="ck_job_attempts_local_worker",
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
        CheckConstraint(
            "(result_manifest_key IS NULL AND result_manifest_sha256 IS NULL) OR "
            "(result_manifest_key IS NOT NULL AND result_manifest_sha256 IS NOT NULL "
            "AND length(result_manifest_key) BETWEEN 1 AND 256 "
            "AND length(result_manifest_sha256) = 64 "
            "AND result_manifest_sha256 NOT GLOB '*[^0-9a-f]*' "
            "AND invocation_id IS NOT NULL AND phase = 'succeeded' "
            "AND finished_at IS NOT NULL AND finished_at >= started_at)",
            name="ck_job_attempts_result_publication",
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
    local_worker_id: Mapped[UUID | None]
    local_worker_pid: Mapped[int | None]
    execution_stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    execution_exit_code: Mapped[int | None]
    result_manifest_key: Mapped[str | None]
    result_manifest_sha256: Mapped[str | None]
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


class RegistrationInvitation(Base):
    __tablename__ = "registration_invitations"
    __table_args__ = (
        UniqueConstraint("code_hash", name="uq_registration_invitations_code_hash"),
        CheckConstraint(
            "length(email) BETWEEN 3 AND 254 AND email = lower(trim(email))",
            name="ck_registration_invitations_email",
        ),
        CheckConstraint(
            "length(code_hash) = 64 AND code_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_registration_invitations_code_hash",
        ),
        CheckConstraint(
            "expires_at > created_at", name="ck_registration_invitations_expiry"
        ),
        CheckConstraint(
            "used_at IS NULL OR (used_at >= created_at AND used_at < expires_at)",
            name="ck_registration_invitations_used_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    email: Mapped[str]
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
