from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_super_admin, page_params
from app.core.utils import oid, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message, PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/notifications", tags=["Super Admin · Notifications"])


@router.get("", summary="Platform notification feed")
async def list_notifications(unread_only: bool = False,
                             tab: str = None, # to support the UI tabs: Requests, Documents, Completed
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(require_super_admin)):
    query = {"user_id": user.id}
    if unread_only:
        query["read"] = False
        
    # Optional filtering for tabs if they map to types
    if tab and tab.lower() != "all":
        # Basic mapping just in case the frontend sends the tab name
        # We might not have these types for admin, but it prevents errors
        pass 

    return await paginate(platform_db(), "platform_notifications", query, params, sort=[("created_at", -1)])


@router.get("/unread-count", summary="Count unread platform notifications")
async def unread_count(user: CurrentUser = Depends(require_super_admin)):
    count = await platform_db().platform_notifications.count_documents({"user_id": user.id, "read": False})
    return {"count": count}


@router.post("/{notification_id}/read", response_model=Message, summary="Mark platform notification read")
async def mark_read(notification_id: str, user: CurrentUser = Depends(require_super_admin)):
    await platform_db().platform_notifications.update_one(
        {"_id": oid(notification_id), "user_id": user.id},
        {"$set": {"read": True, "read_at": utcnow()}}
    )
    return {"detail": "Marked as read"}


@router.post("/read-all", response_model=Message, summary="Mark all platform notifications read")
async def mark_all_read(user: CurrentUser = Depends(require_super_admin)):
    await platform_db().platform_notifications.update_many(
        {"user_id": user.id, "read": False},
        {"$set": {"read": True, "read_at": utcnow()}}
    )
    return {"detail": "All notifications marked as read"}
