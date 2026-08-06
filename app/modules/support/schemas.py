from typing import Optional

from pydantic import BaseModel, Field

from app.core.enums import TicketCategory, TicketPriority


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    message: str = Field(min_length=3, max_length=8000)
    category: TicketCategory = TicketCategory.OTHER
    priority: TicketPriority = TicketPriority.NORMAL
    case_reference: Optional[str] = None


class TicketReply(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
