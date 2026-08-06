from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.core.enums import TaskAssigneeType, TaskStatus


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    description: Optional[str] = None
    case_id: str
    assignee_id: str
    assignee_type: TaskAssigneeType = TaskAssigneeType.PARTNER
    due_date: Optional[datetime] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[datetime] = None
    status: Optional[TaskStatus] = None


class TaskStatusUpdate(BaseModel):
    status: TaskStatus
    note: Optional[str] = None


class TaskOut(BaseModel):
    id: str
    title: str
    description: Optional[str] = None
    case_id: Optional[str] = None
    case_reference: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    assignee_id: str
    assignee_name: Optional[str] = None
    assignee_type: TaskAssigneeType
    status: TaskStatus
    due_date: Optional[datetime] = None
    auto_created: bool = False
    deliverables: list = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
