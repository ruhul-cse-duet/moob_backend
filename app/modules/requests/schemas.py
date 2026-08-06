from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.enums import DocumentCategory, RequestStatus


class RequestedDocument(BaseModel):
    name: str
    category: DocumentCategory = DocumentCategory.OTHER
    why: Optional[str] = None
    due_date: Optional[datetime] = None


class RequestCreate(BaseModel):
    """Submitted by the client from the mobile app."""
    visa_type: str = Field(min_length=2, max_length=80)
    destination_country: str
    purpose: str = Field(min_length=5, max_length=2000)
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None
    consultant_id: Optional[str] = None


class RequestUpdate(BaseModel):
    visa_type: Optional[str] = None
    destination_country: Optional[str] = None
    purpose: Optional[str] = None
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None


class RequestDocumentsRequest(BaseModel):
    documents: List[RequestedDocument] = Field(min_length=1)
    message: Optional[str] = None


class ReviewNotes(BaseModel):
    notes: str = Field(max_length=8000)


class RequestOut(BaseModel):
    id: str
    reference: str
    visa_type: str
    destination_country: str
    origin_country: Optional[str] = None
    purpose: str
    status: RequestStatus
    client_id: str
    client_name: Optional[str] = None
    consultant_id: Optional[str] = None
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    review_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None
    attached_files: List[dict] = []
    documents_total: int = 0
    documents_approved: int = 0
    documents_awaiting_review: int = 0
    case_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RequestCounts(BaseModel):
    new: int = 0
    waiting_for_client: int = 0
    documents_received: int = 0
    under_review: int = 0
    completed: int = 0
