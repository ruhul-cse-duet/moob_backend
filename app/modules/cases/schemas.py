from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.core.enums import CaseStage


class CaseCreate(BaseModel):
    """A case is a client plus the procedure the consultant assigned.

    `case_type` was required and is not any more: when a procedure is named, its
    own name is the type, and making the caller repeat it is how the two drift
    apart. It stays for a case opened without a procedure - a consultation with
    no catalogue entry yet.
    """
    client_id: str
    process_area: Optional[str] = Field(
        None, description="Area key, e.g. `immigration`, `labour`")
    procedure_id: Optional[str] = Field(
        None, description="A procedure from this organization's catalogue")
    case_type: Optional[str] = Field(
        None, description="Free-text label when no procedure is assigned")
    destination_country: Optional[str] = None
    deadline: Optional[datetime] = None
    request_id: Optional[str] = None


class CaseUpdate(BaseModel):
    case_type: Optional[str] = None
    deadline: Optional[datetime] = None
    consultant_id: Optional[str] = None


class AdvanceStage(BaseModel):
    stage: Optional[CaseStage] = None   # omit to move to the next stage in order
    note: Optional[str] = None


class ChecklistItem(BaseModel):
    title: str
    description: Optional[str] = None
    due_date: Optional[datetime] = None


class CaseOut(BaseModel):
    id: str
    reference: str
    client_id: str
    client_name: Optional[str] = None
    consultant_id: Optional[str] = None
    partner_id: Optional[str] = None
    consultant: Optional[Dict[str, Any]] = None
    case_type: str
    destination_country: Optional[str] = None
    stage: CaseStage
    progress: int = 0
    deadline: Optional[datetime] = None
    timeline: List[Dict[str, Any]] = []
    ai_guidance: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
