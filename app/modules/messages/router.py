from typing import List, Optional

from fastapi import APIRouter, Body, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
)
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/messages", tags=["Messages"],
                   dependencies=[Depends(require_active_tenant)])


class ThreadCreate(BaseModel):
    participant_ids: List[str]
    case_id: Optional[str] = None
    subject: Optional[str] = None


@router.post("/threads", status_code=201)
async def create_thread(payload: ThreadCreate,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    participants = sorted(set(payload.participant_ids + [user.id]))
    existing = await db.threads.find_one({"participant_ids": participants,
                                          "case_id": payload.case_id})
    if existing:
        return serialize(existing)
    now = utcnow()
    doc = {"participant_ids": participants, "case_id": payload.case_id,
           "subject": payload.subject, "last_message": None,
           "created_at": now, "updated_at": now}
    result = await db.threads.insert_one(doc)
    return serialize({**doc, "_id": result.inserted_id})


@router.get("/threads")
async def list_threads(params: PageParams = Depends(page_params),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "threads", {"participant_ids": user.id}, params,
                          sort=[("updated_at", -1)])


@router.get("/threads/{thread_id}")
async def thread_messages(thread_id: str, params: PageParams = Depends(page_params),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Thread not found")
    if user.id not in thread["participant_ids"]:
        raise Forbidden("You are not part of this conversation")
    return await paginate(db, "messages", {"thread_id": thread_id}, params,
                          sort=[("created_at", 1)])


@router.post("/threads/{thread_id}", status_code=201)
async def send_message(thread_id: str, body: str = Body(embed=True),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Thread not found")
    if user.id not in thread["participant_ids"]:
        raise Forbidden("You are not part of this conversation")
    now = utcnow()
    doc = {"thread_id": thread_id, "sender_id": user.id,
           "sender_name": user.raw.get("full_name"), "body": body,
           "read_by": [user.id], "created_at": now}
    result = await db.messages.insert_one(doc)
    await db.threads.update_one({"_id": oid(thread_id)},
                                {"$set": {"last_message": body[:140], "updated_at": now}})
    return serialize({**doc, "_id": result.inserted_id})
