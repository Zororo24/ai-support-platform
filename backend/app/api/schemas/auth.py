from uuid import UUID

from pydantic import BaseModel, EmailStr

from app.db.models import UserRole


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: UUID
    email: EmailStr
    role: UserRole
    tenant_id: UUID
    tenant_slug: str
    tenant_name: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
