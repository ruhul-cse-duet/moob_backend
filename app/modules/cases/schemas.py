from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.core.enums import CaseStage


class CaseCreate(BaseModel):
    client_id: str
    case_type: str
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
