from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.core.enums import Role, UserStatus


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=120)
    mobile: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    nationality: Optional[str] = None
    country_of_residence: Optional[str] = None
    avatar_url: Optional[str] = None


class TeamInvite(BaseModel):
    full_name: str
    email: EmailStr
    mobile: Optional[str] = None
    title: str = "Consultant"


class ClientCreate(BaseModel):
    full_name: str
    email: EmailStr
    mobile: str
    nationality: Optional[str] = None
    language: Optional[str] = None
    country_of_residence: Optional[str] = None


class UserOut(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    mobile: Optional[str] = None
    role: Role
    title: Optional[str] = None
    status: UserStatus
    avatar_url: Optional[str] = None
    language: Optional[str] = None
    created_at: Optional[datetime] = None
