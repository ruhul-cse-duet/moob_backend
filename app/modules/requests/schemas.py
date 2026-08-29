from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.enums import DocumentCategory, RequestStatus


class RequestedDocument(BaseModel):
    name: str
    category: DocumentCategory = DocumentCategory.OTHER
    why: Optional[str] = None
    is_required: bool = True
    due_date: Optional[datetime] = None


class RequestCreate(BaseModel):
    """Submitted by the client from the mobile app."""
    visa_type: str = Field(min_length=2, max_length=80)
    destination_country: str
    purpose: str = Field(min_length=2, max_length=2000)
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None
    consultant_id: Optional[str] = None
    attached_files: Optional[List[dict]] = Field(default_factory=list)
    is_draft: bool = False


class RequestUpdate(BaseModel):
    visa_type: Optional[str] = None
    destination_country: Optional[str] = None
    purpose: Optional[str] = None
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None
    attached_files: Optional[List[dict]] = None
    is_draft: Optional[bool] = None


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
    # No `status_label` here on purpose. `status` is the enum code; the app
    # holds the wording for it in each language. A label built server-side
    # arrives as opaque English text that no translation file can reach.
    status_steps: List[dict] = []
    client_id: str
    client_name: Optional[str] = None
    consultant_id: Optional[str] = None
    partner_id: Optional[str] = None
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    review_notes: Optional[str] = None
    preferred_appointment: Optional[str] = "Flexible"
    attached_files: List[dict] = []
    documents_total: int = 0
    documents_approved: int = 0
    documents_awaiting_review: int = 0
    documents_action_required: int = 0
    is_draft: bool = False
    progress_percentage: int = 0
    document_stats: Optional[dict] = None
    documents: List[dict] = []
    client_profile: Optional[dict] = None
    case_id: Optional[str] = None
    # What the consultant concluded once the consultation closed.
    outcome: Optional[dict] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None



class ConsultationOutcomeIn(BaseModel):
    """What the consultant concluded — the client reads this on the request."""
    summary: str = Field(min_length=2, max_length=4000)
    guidance: Optional[str] = None
    next_steps: List[str] = []
    notes: Optional[str] = None
    timeline: Optional[str] = None
    recommendations: Optional[str] = None


class CompleteConsultation(BaseModel):
    outcome: ConsultationOutcomeIn
    case_type: Optional[str] = None
    deadline: Optional[datetime] = None


class RequestCounts(BaseModel):
    new: int = 0
    waiting_for_client: int = 0
    documents_received: int = 0
    under_review: int = 0
    completed: int = 0


class ClientDashboardSummary(BaseModel):
    client_name: str
    unread_notifications_count: int = 0
    active_hero_request: Optional[dict] = None
    action_next_step: Optional[dict] = None
    my_requests: List[dict] = []
    recent_activities: List[dict] = []
    pending_documents_count: int = 0


class ClientRequestCategory(BaseModel):
    id: str
    name: str
    icon: str
    description: Optional[str] = None


class ConsultantDashboardSummary(BaseModel):
    consultant_name: str
    unread_notifications_count: int = 0
    todays_count: int = 0
    to_review_count: int = 0
    waiting_count: int = 0
    client_request_queue_banner: dict
    open_requests: List[dict] = []
    recent_activity: List[dict] = []


