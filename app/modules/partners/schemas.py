from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.core.validators import Email, Phone

from app.core.enums import UserStatus


class PartnerInvite(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: Email
    mobile: Phone
    role: str = Field(description="e.g. Certified Translator, Notary, Legal Document Specialist")


class PartnerOut(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    # Plain str, not `Phone`: this describes what is stored, and rows
    # written before the rules existed still have to be readable. A
    # validator here turns one old number into a 500 on the whole
    # endpoint, which is a worse answer than an unnormalised string.
    mobile: Optional[str] = None
    partner_role: Optional[str] = None
    status: UserStatus
    consultant_id: Optional[str] = None
    joined_at: Optional[datetime] = None
    invited_at: Optional[datetime] = None
    open_tasks: int = 0
    invite_token: Optional[str] = None
    invite_link: Optional[str] = None
    invite_expires_in_days: Optional[int] = None
    # False when SMTP refused the message. The invitation still exists - the
    # code below it is the way in - but nobody has been told about it.
    invite_email_sent: Optional[bool] = None


class InviteResent(BaseModel):
    detail: str
    invite_token: Optional[str] = None
    invite_link: Optional[str] = None
    invite_email_sent: Optional[bool] = None


class SeatUsage(BaseModel):
    consultant_seats_used: int
    consultant_seats_limit: Optional[int]
    partner_seats_used: int
    partner_seats_limit: Optional[int]
