from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
)
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import Message, PageParams
from app.services.events import notify
from app.core.enums import NotificationType
from app.services.pagination import paginate

router = APIRouter(prefix="/agenda", tags=["Agenda & Appointments"],
                   dependencies=[Depends(require_active_tenant)])


class AppointmentCreate(BaseModel):
    title: str
    client_id: Optional[str] = None
    request_id: Optional[str] = None
    case_id: Optional[str] = None
    starts_at: datetime
    duration_minutes: int = 30
    location: Optional[str] = None
    notes: Optional[str] = None


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_appointment(payload: AppointmentCreate,
                             user: CurrentUser = Depends(require_consultant),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    doc = {**payload.model_dump(), "consultant_id": user.id,
           "ends_at": payload.starts_at + timedelta(minutes=payload.duration_minutes),
           "status": "scheduled", "created_at": now, "updated_at": now}
    result = await db.appointments.insert_one(doc)
    if payload.client_id:
        await notify(db, user_ids=[payload.client_id],
                     type=NotificationType.CASE_STAGE_CHANGED,
                     title="Appointment scheduled", body=payload.title,
                     data={"appointment_id": str(result.inserted_id)})
    return serialize({**doc, "_id": result.inserted_id})


@router.get("", summary="Agenda for a date range")
async def list_appointments(date_from: Optional[datetime] = Query(None),
                            date_to: Optional[datetime] = Query(None),
                            params: PageParams = Depends(page_params),
                            user: CurrentUser = Depends(get_current_user),
                            db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    query = {"$or": [{"consultant_id": user.id}, {"client_id": user.id}]}
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        query["starts_at"] = rng
    return await paginate(db, "appointments", query, params, sort=[("starts_at", 1)])


@router.delete("/{appointment_id}", response_model=Message)
async def cancel(appointment_id: str,
                 user: CurrentUser = Depends(require_consultant),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.appointments.update_one({"_id": oid(appointment_id)},
                                     {"$set": {"status": "cancelled",
                                               "updated_at": utcnow()}})
    return {"detail": "Appointment cancelled"}
