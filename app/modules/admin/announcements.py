"""Helpdesk & Announcements + platform-wide Notifications for Super Admin."""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status as http
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import AuditAction
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message, PageParams
from app.services import audit
from app.services.pagination import paginate

router = APIRouter(tags=["Super Admin · Announcements & Notifications"])


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=8000)
    audience: str = Field(
        default="all",
        description="all | consultants | partners | clients",
    )
    severity: str = Field(default="info", description="info | warning | critical")
    published: bool = True


class AnnouncementUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    audience: Optional[str] = None
    severity: Optional[str] = None
    published: Optional[bool] = None


class AdminNotificationCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    link: Optional[str] = None
    severity: str = Field(default="info")
    admin_ids: Optional[List[str]] = Field(
        default=None, description="Target admin ids; defaults to the caller")


# ---------- Announcements (broadcast) ----------
@router.get("/announcements", summary="Platform announcements")
async def list_announcements(published: Optional[bool] = Query(None),
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(require_super_admin)):
    query = {}
    if published is not None:
        query["published"] = published
    return await paginate(platform_db(), "announcements", query, params,
                          sort=[("created_at", -1)])


@router.post("/announcements", status_code=http.HTTP_201_CREATED,
             summary="Publish a platform announcement")
async def create_announcement(payload: AnnouncementCreate,
                              user: CurrentUser = Depends(require_super_admin)):
    now = utcnow()
    doc = {
        **payload.model_dump(),
        "created_by": user.id,
        "created_by_name": user.raw.get("full_name"),
        "created_at": now,
        "updated_at": now,
        "published_at": now if payload.published else None,
    }
    ann_id = str((await platform_db().announcements.insert_one(doc)).inserted_id)
    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject="announcement",
                       detail=payload.title)
    return serialize({**doc, "_id": oid(ann_id)})


@router.patch("/announcements/{announcement_id}", summary="Update an announcement")
async def update_announcement(announcement_id: str, payload: AnnouncementUpdate,
                              user: CurrentUser = Depends(require_super_admin)):
    data = {k: v for k, v in payload.model_dump(exclude_unset=True).items()
            if v is not None}
    data["updated_at"] = utcnow()
    if data.get("published") is True:
        data.setdefault("published_at", utcnow())
    result = await platform_db().announcements.find_one_and_update(
        {"_id": oid(announcement_id)}, {"$set": data}, return_document=True)
    if not result:
        raise NotFound("Announcement not found")
    return serialize(result)


@router.delete("/announcements/{announcement_id}", response_model=Message)
async def delete_announcement(announcement_id: str,
                              user: CurrentUser = Depends(require_super_admin)):
    result = await platform_db().announcements.delete_one({"_id": oid(announcement_id)})
    if not result.deleted_count:
        raise NotFound("Announcement not found")
    return {"success": True, "message": "Announcement deleted",
            "detail": "Announcement deleted"}


# ---------- Admin notification inbox (bell) ----------
@router.get("/notifications", summary="Platform admin notification inbox")
async def list_notifications(unread_only: bool = Query(False),
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(require_super_admin)):
    query: dict = {"admin_id": user.id}
    if unread_only:
        query["read"] = False
    return await paginate(platform_db(), "admin_notifications", query, params,
                          sort=[("created_at", -1)])


@router.post("/notifications", status_code=http.HTTP_201_CREATED,
             summary="Create a notification for platform staff (or self)")
async def create_notification(payload: AdminNotificationCreate,
                              user: CurrentUser = Depends(require_super_admin)):
    targets = payload.admin_ids or [user.id]
    now = utcnow()
    docs = [{
        "admin_id": admin_id,
        "title": payload.title,
        "body": payload.body,
        "link": payload.link,
        "severity": payload.severity,
        "read": False,
        "created_by": user.id,
        "created_at": now,
    } for admin_id in targets]
    if docs:
        await platform_db().admin_notifications.insert_many(docs)
    return {
        "success": True,
        "message": f"{len(docs)} notification(s) created",
        "detail": f"{len(docs)} notification(s) created",
        "count": len(docs),
    }


@router.post("/notifications/read-all", response_model=Message)
async def mark_all_notifications_read(user: CurrentUser = Depends(require_super_admin)):
    result = await platform_db().admin_notifications.update_many(
        {"admin_id": user.id, "read": False},
        {"$set": {"read": True, "read_at": utcnow()}})
    return {
        "success": True,
        "message": f"{result.modified_count} notification(s) marked as read",
        "detail": f"{result.modified_count} notification(s) marked as read",
    }


@router.post("/notifications/{notification_id}/read", response_model=Message)
async def mark_notification_read(notification_id: str,
                                 user: CurrentUser = Depends(require_super_admin)):
    result = await platform_db().admin_notifications.update_one(
        {"_id": oid(notification_id), "admin_id": user.id},
        {"$set": {"read": True, "read_at": utcnow()}})
    if not result.matched_count:
        raise NotFound("Notification not found")
    return {"success": True, "message": "Marked as read", "detail": "Marked as read"}
