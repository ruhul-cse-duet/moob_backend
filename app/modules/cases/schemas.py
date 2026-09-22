from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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
    # A free string, not the fixed `CaseStage` enum: a case opened against a
    # procedure moves through *that procedure's own* stage keys (see
    # `service._stage_order`), which the platform's 11-value enum knows
    # nothing about. `service.advance_stage` validates it against the case's
    # own stage list instead. Omit to move to the next stage in that order.
    stage: Optional[str] = None
    note: Optional[str] = None
    # The review's own words: "each stage must have an owner and a deadline".
    # Both optional - a consultant working alone advancing their own case has
    # nothing new to say - but when set, `owner_id` becomes who the workspace
    # holds responsible for what happens next, and `deadline` is mirrored onto
    # the calendar (see `service.advance_stage`), not just left as a note on
    # the timeline.
    owner_id: Optional[str] = None
    deadline: Optional[datetime] = None
    # C6: moving into the last stage of the workflow is refused while a
    # required document is still unapproved, unless the consultant explicitly
    # confirms closing anyway.
    force: bool = False


class FormFieldUpdate(BaseModel):
    """A consultant correcting or completing one field by hand - see the
    review's item 7.3: the AI fills the form, the consultant reviews it."""
    value: Any


class AuthorityRequest(BaseModel):
    """The repeatable half of the workflow: an authority may come back asking
    for more before it decides, more than once, without the case restarting.
    Each call is one round of that cycle - logged to the case history with
    who is answering it and by when, rather than overwriting the last one.
    """
    note: str = Field(min_length=1, description="What the authority is asking for")
    response_due: Optional[datetime] = None


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
    # A free string - see the note on `AdvanceStage.stage`. It was the fixed
    # `CaseStage` enum; a procedure-driven case's own stage key (e.g.
    # "documents") failed that validation and would have made this response
    # a 500 instead of the case it is meant to return.
    stage: str
    progress: int = 0
    deadline: Optional[datetime] = None
    # Set by `advance_stage` alongside the stage itself - see the schema note
    # on `AdvanceStage`. Declared here too: `response_model` silently drops any
    # field it does not list, and this is the same shape that endpoint returns.
    stage_owner_id: Optional[str] = None
    stage_deadline: Optional[datetime] = None
    authority_requests: List[Dict[str, Any]] = []
    # The procedure's own stages and documents (item C1) - also declared here
    # so `response_model` does not strip them from what `advance_stage` and
    # `update_case` return.
    workflow_stages: List[Dict[str, Any]] = []
    required_documents: List[Dict[str, Any]] = []
    client_fields: List[Dict[str, Any]] = []
    timeline: List[Dict[str, Any]] = []
    ai_guidance: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
