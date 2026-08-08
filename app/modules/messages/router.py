from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query
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


# ─── Schemas ───────────────────────────────────────────────────────────
class ThreadCreate(BaseModel):
    participant_ids: List[str]
    case_id: Optional[str] = None
    subject: Optional[str] = None


class SendMessage(BaseModel):
    body: str
    attachment_url: Optional[str] = None
    attachment_type: Optional[str] = None  # image, pdf, audio


# ─── Thread CRUD ───────────────────────────────────────────────────────
@router.post("/threads", status_code=201, summary="Create or reuse a conversation thread")
async def create_thread(payload: ThreadCreate,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    participants = sorted(set(payload.participant_ids + [user.id]))
    existing = await db.threads.find_one({"participant_ids": participants,
                                          "case_id": payload.case_id})
    if existing:
        return serialize(existing)
    now = utcnow()
    consultant_id = user.consultant_id or user.id
    doc = {
        "participant_ids": participants,
        "consultant_id": consultant_id,
        "case_id": payload.case_id,
        "subject": payload.subject,
        "last_message": None,
        "last_message_at": None,
        "unread_counts": {pid: 0 for pid in participants},
        "created_at": now,
        "updated_at": now,
    }
    result = await db.threads.insert_one(doc)
    return serialize({**doc, "_id": result.inserted_id})


@router.get("/threads", summary="List all conversation threads for the current user")
async def list_threads(params: PageParams = Depends(page_params),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    page = await paginate(db, "threads", {"participant_ids": user.id}, params,
                          sort=[("updated_at", -1)])
    # Enrich each thread with participant details for the mobile UI
    for item in page.get("items", []):
        enriched = []
        for pid in item.get("participant_ids", []):
            p = await db.users.find_one({"_id": oid(pid)})
            if p:
                enriched.append({
                    "id": str(p["_id"]),
                    "full_name": p.get("full_name", ""),
                    "avatar_url": p.get("avatar_url"),
                    "role": p.get("role"),
                    "is_online": p.get("is_online", False),
                })
        item["participants"] = enriched
    return page


@router.get("/threads/{thread_id}", summary="Get messages in a conversation thread")
async def thread_messages(thread_id: str,
                          params: PageParams = Depends(page_params),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Thread not found")
    if user.id not in thread["participant_ids"]:
        raise Forbidden("You are not part of this conversation")

    # Mark all messages as read by this user
    await db.messages.update_many(
        {"thread_id": thread_id, "read_by": {"$ne": user.id}},
        {"$addToSet": {"read_by": user.id}},
    )
    # Reset unread count for this user
    await db.threads.update_one(
        {"_id": oid(thread_id)},
        {"$set": {f"unread_counts.{user.id}": 0}},
    )

    # Enrich with participant profile for header display
    enriched_participants = []
    for pid in thread["participant_ids"]:
        p = await db.users.find_one({"_id": oid(pid)})
        if p:
            enriched_participants.append({
                "id": str(p["_id"]),
                "full_name": p.get("full_name", ""),
                "avatar_url": p.get("avatar_url"),
                "role": p.get("role"),
                "is_online": p.get("is_online", False),
            })

    messages = await paginate(db, "messages", {"thread_id": thread_id}, params,
                              sort=[("created_at", 1)])
    return {
        "thread": serialize(thread),
        "participants": enriched_participants,
        "consultant_id": thread.get("consultant_id"),
        **messages,
    }


@router.post("/threads/{thread_id}", status_code=201, summary="Send a message in a conversation")
async def send_message(thread_id: str,
                       payload: SendMessage,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Thread not found")
    if user.id not in thread["participant_ids"]:
        raise Forbidden("You are not part of this conversation")
    now = utcnow()
    doc = {
        "thread_id": thread_id,
        "sender_id": user.id,
        "sender_name": user.raw.get("full_name", ""),
        "sender_avatar": user.raw.get("avatar_url"),
        "sender_role": user.raw.get("role"),
        "body": payload.body,
        "attachment_url": payload.attachment_url,
        "attachment_type": payload.attachment_type,
        "read_by": [user.id],
        "delivered": True,
        "created_at": now,
    }
    result = await db.messages.insert_one(doc)

    # Update thread metadata for listing
    unread_inc = {f"unread_counts.{pid}": 1
                  for pid in thread["participant_ids"] if pid != user.id}
    await db.threads.update_one(
        {"_id": oid(thread_id)},
        {
            "$set": {
                "last_message": payload.body[:140],
                "last_message_at": now,
                "updated_at": now,
            },
            "$inc": unread_inc,
        },
    )
    return serialize({**doc, "_id": result.inserted_id})


@router.post("/threads/{thread_id}/read", summary="Mark all messages in a thread as read")
async def mark_read(thread_id: str,
                    user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    thread = await db.threads.find_one({"_id": oid(thread_id)})
    if not thread:
        raise NotFound("Thread not found")
    if user.id not in thread["participant_ids"]:
        raise Forbidden("You are not part of this conversation")
    await db.messages.update_many(
        {"thread_id": thread_id, "read_by": {"$ne": user.id}},
        {"$addToSet": {"read_by": user.id}},
    )
    await db.threads.update_one(
        {"_id": oid(thread_id)},
        {"$set": {f"unread_counts.{user.id}": 0}},
    )
    return {"detail": "All messages marked as read"}

