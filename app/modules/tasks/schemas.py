from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.core.enums import TaskAssigneeType, TaskStatus


class ReferenceFile(BaseModel):
    file_name: str
    file_url: str
    file_type: Optional[str] = None  # pdf, image, doc


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: Optional[str] = None
    case_id: str
    assignee_id: str
    assignee_type: TaskAssigneeType = TaskAssigneeType.PARTNER
    due_date: Optional[datetime] = None
    reference_files: List[ReferenceFile] = []


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[TaskStatus] = None


class TaskStatusUpdate(BaseModel):
    status: TaskStatus
    note: Optional[str] = None


class TaskComplete(BaseModel):
    """Partner marks task as completed with optional delivery notes."""
    delivery_notes: Optional[str] = None


class TaskOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    case_id: Optional[str] = None
    case_reference: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    consultant_id: Optional[str] = None
    partner_id: Optional[str] = None
    assignee_id: str
    assignee_name: Optional[str] = None
    assignee_type: TaskAssigneeType
    status: TaskStatus
    due_date: Optional[datetime] = None
    auto_created: bool = False
    reference_files: List[dict] = []
    deliverables: list = []
    delivery_notes: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskSummary(BaseModel):
    total: int
    pending: int
    in_progress: int
    submitted: int
    completed: int


class PartnerDashboardOut(BaseModel):
    partner_id: str
    consultant_id: Optional[str] = None
    summary: TaskSummary
    active_tasks: List[TaskOut]
