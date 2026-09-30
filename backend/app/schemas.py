from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str


class UploadResponse(BaseModel):
    id: UUID
    filename: str
    content_type: str


class CreateJobRequest(BaseModel):
    upload_id: UUID


class JobResponse(BaseModel):
    id: UUID
    status: str
    upload_id: UUID
    outputs: dict[str, str]
