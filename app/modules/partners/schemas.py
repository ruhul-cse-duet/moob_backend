from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.validators import Email, OptionalPhone, Phone

from app.core.enums import UserStatus


class PartnerInvite(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: Email
    mobile: Phone
    role: str = Field(description="e.g. Certified Translator, Notary, Legal Document Specialist")


class PartnerOut(BaseModel):
    id: str
    full_name: str
    email: Email
    mobile: OptionalPhone = None
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
