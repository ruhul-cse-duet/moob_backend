from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import CurrentUser, get_current_user, get_tenant_db, page_params
from app.core.utils import oid, utcnow
from app.schemas.common import Message, PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("", summary="Notification feed")
async def list_notifications(unread_only: bool = False,
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(get_current_user),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    query = {"user_id": user.id}
    if unread_only:
        query["read"] = False
    return await paginate(db, "notifications", query, params, sort=[("created_at", -1)])


@router.get("/unread-count")
async def unread_count(user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"count": await db.notifications.count_documents({"user_id": user.id, "read": False})}


@router.post("/{notification_id}/read", response_model=Message)
async def mark_read(notification_id: str,
                    user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.notifications.update_one({"_id": oid(notification_id), "user_id": user.id},
                                      {"$set": {"read": True, "read_at": utcnow()}})
    return {"detail": "Marked as read"}


@router.post("/read-all", response_model=Message)
async def mark_all_read(user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.notifications.update_many({"user_id": user.id, "read": False},
                                       {"$set": {"read": True, "read_at": utcnow()}})
    return {"detail": "All notifications marked as read"}
