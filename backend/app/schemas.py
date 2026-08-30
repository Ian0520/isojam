from pydantic import BaseModel, EmailStr
from uuid import UUID

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

class UserResponse(BaseModel):
    id: UUID
    email: EmailStr