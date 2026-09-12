from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class ProcessAreaIn(BaseModel):
    """A field of practice the organization works in.

    `key` is what requests, cases and reports are filtered by, so it is stable
    and machine-readable; `name` is what a person reads and may be renamed
    freely without moving any existing case.
    """
    key: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_]+$",
                     description="Stable id, e.g. `immigration`, `labour`, `tax`")
    name: str = Field(min_length=2, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    icon: Optional[str] = Field(None, max_length=40)
    active: bool = True


class ProcessAreaUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    description: Optional[str] = Field(None, max_length=500)
    icon: Optional[str] = Field(None, max_length=40)
    active: Optional[bool] = None


class ProcessAreaOut(BaseModel):
    id: str
    key: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    active: bool = True
    built_in: bool = False
    procedure_count: int = 0
    created_at: Optional[datetime] = None


class ClientField(BaseModel):
    """One thing the client is asked for, on a form this procedure defines.

    Immigration wants a passport number; a dismissal claim wants an employer and
    a termination date. Neither belongs in a form every client sees, which is
    what "Your immigration profile" was.
    """
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    type: str = Field("text", pattern=r"^(text|number|date|select|country|textarea|file)$")
    required: bool = False
    options: List[str] = Field(default_factory=list,
                               description="For `select` only")
    help_text: Optional[str] = Field(None, max_length=300)


class RequiredDocument(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    category: Optional[str] = Field(None, max_length=40)
    why: Optional[str] = Field(None, max_length=300)
    mandatory: bool = True


class WorkflowStage(BaseModel):
    key: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=80)
    #: Days from the stage starting. What the deadline on a case is counted from.
    duration_days: Optional[int] = Field(None, ge=0, le=3650)


class ProcedureIn(BaseModel):
    area_key: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=1000)
    required_documents: List[RequiredDocument] = Field(default_factory=list)
    client_fields: List[ClientField] = Field(default_factory=list)
    workflow_stages: List[WorkflowStage] = Field(default_factory=list)
    default_deadline_days: Optional[int] = Field(None, ge=0, le=3650)
    active: bool = True


class ProcedureUpdate(BaseModel):
    area_key: Optional[str] = None
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=1000)
    required_documents: Optional[List[RequiredDocument]] = None
    client_fields: Optional[List[ClientField]] = None
    workflow_stages: Optional[List[WorkflowStage]] = None
    default_deadline_days: Optional[int] = Field(None, ge=0, le=3650)
    active: Optional[bool] = None


class ProcedureOut(BaseModel):
    id: str
    area_key: str
    area_name: Optional[str] = None
    name: str
    description: Optional[str] = None
    required_documents: List[dict] = Field(default_factory=list)
    client_fields: List[dict] = Field(default_factory=list)
    workflow_stages: List[dict] = Field(default_factory=list)
    default_deadline_days: Optional[int] = None
    active: bool = True
    from_template: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class TemplateOut(BaseModel):
    key: str
    area_key: str
    name: str
    description: Optional[str] = None
    required_documents: List[dict] = Field(default_factory=list)
    client_fields: List[dict] = Field(default_factory=list)
    workflow_stages: List[dict] = Field(default_factory=list)


class DuplicateProcedure(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120,
                                description="Defaults to the original plus (copy)")
