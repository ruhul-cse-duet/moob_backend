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
    """Opened by the consultant, for one of their clients.

    The procedure is what the request is *about*, and it comes from the
    organization's own catalogue. `visa_type` and `destination_country` are kept
    for the immigration procedures that want them, and for the requests that
    already carry them - neither is required any more, because a dismissal claim
    has no visa type and never did.
    """
    client_id: Optional[str] = Field(
        None, description="The client this request is for")
    process_area: Optional[str] = Field(
        None, description="Area key, e.g. `immigration`, `labour`")
    procedure_id: Optional[str] = Field(
        None, description="A procedure from this organization's catalogue")
    visa_type: Optional[str] = Field(
        None, max_length=80,
        description="Immigration procedures only. Falls back to the procedure name.")
    destination_country: Optional[str] = Field(
        None, description="Immigration procedures only")
    purpose: str = Field(min_length=2, max_length=2000)
    additional_information: Optional[str] = None
    client_notes: Optional[str] = None
    preferred_appointment: Optional[str] = None
    consultant_id: Optional[str] = None
    attached_files: Optional[List[dict]] = Field(default_factory=list)
    is_draft: bool = False


class RequestDecline(BaseModel):
    reason: str = Field(min_length=3, max_length=1000,
                        description="Shown to the client, so write it for them")


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
    # Immigration procedures only. A labour or tax request never had one to
    # begin with, so this has to be optional or a response for one 500s on
    # pydantic validation the moment `destination_country` is genuinely absent.
    destination_country: Optional[str] = None
    origin_country: Optional[str] = None
    purpose: str
    status: RequestStatus
    # What the consultant assigned from the organization's own catalogue —
    # absent when the request predates it, or names no procedure. The app
    # uses `process_area` to decide whether immigration-only fields like
    # `destination_country` mean anything for this particular request.
    process_area: Optional[str] = None
    procedure_id: Optional[str] = None
    procedure_name: Optional[str] = None
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


class OpenCase(BaseModel):
    """What the consultant assigns when they take the work on.

    Every field is optional: the request already carries what was agreed, and a
    consultant who just wants the case open should not have to retype it.
    """
    process_area: Optional[str] = Field(
        None, description="Immigration, Labour, Civil, Tax - or a custom area")
    procedure_id: Optional[str] = Field(
        None, description="A procedure from this organization's catalogue")
    case_type: Optional[str] = Field(
        None, description="Free-text label when no procedure is assigned")
    deadline: Optional[datetime] = None


class CompleteConsultation(BaseModel):
    outcome: ConsultationOutcomeIn
    case_type: Optional[str] = None
    deadline: Optional[datetime] = None


class RequestCounts(BaseModel):
    pending_approval: int = 0
    new: int = 0
    waiting_for_client: int = 0
    documents_received: int = 0
    under_review: int = 0
    completed: int = 0
    declined: int = 0

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


