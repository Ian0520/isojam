from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import ForeignKey
from uuid import UUID, uuid4

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

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4) 
    upload_id: Mapped[UUID] = mapped_column(ForeignKey("uploads.id"))
    status: Mapped[str] 

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