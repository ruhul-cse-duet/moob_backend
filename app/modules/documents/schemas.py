from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from app.core.enums import DocumentCategory, DocumentStatus


class DocumentOut(BaseModel):
    id: str
    name: str
    category: DocumentCategory
    status: DocumentStatus
    why: Optional[str] = None
    due_date: Optional[datetime] = None
    request_id: Optional[str] = None
    case_id: Optional[str] = None
    client_id: str
    consultant_id: Optional[str] = None
    partner_id: Optional[str] = None
    file: Optional[Dict[str, Any]] = None
    ai_analysis: Optional[Dict[str, Any]] = None
    consultant_feedback: Optional[str] = None
    popup_modal: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RejectPayload(BaseModel):
    feedback: str = Field(min_length=3, max_length=1000)


class CommentPayload(BaseModel):
    comment: str = Field(min_length=1, max_length=1000)
