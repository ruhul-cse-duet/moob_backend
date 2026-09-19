"""The calendar screen and the consultant's "what is due" agenda.

Answers the client's own message directly: a place that reads every date the
workspace already knows about, tracks it, and can tell a consultant what is
due today, this week or this month - rather than each date sitting as plain
text on the one screen that happens to show it.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import CurrentUser, get_current_user, get_tenant_db, require_active_tenant
from app.modules.deadlines import service
from app.schemas.common import Message

router = APIRouter(prefix="/deadlines", tags=["Deadlines & Calendar"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("", summary="Every open deadline in range - the calendar's data source")
async def list_deadlines(date_from: Optional[datetime] = Query(None),
                         date_to: Optional[datetime] = Query(None),
                         kind: Optional[str] = Query(None),
                         include_done: bool = Query(False),
                         user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    items = await service.list_deadlines(db, user, date_from=date_from, date_to=date_to,
                                         kind=kind, include_done=include_done)
    return {"items": items, "total": len(items)}


@router.get("/summary", summary="Due today / this week / this month / overdue")
async def deadline_summary(user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.summary(db, user)


@router.patch("/{deadline_id}/dismiss", response_model=Message)
async def dismiss_deadline(deadline_id: str,
                           user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await service.dismiss(db, user, deadline_id)
    return {"detail": "Deadline dismissed"}
