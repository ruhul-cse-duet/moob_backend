"""Notification + activity feed helpers used by every domain module."""
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.enums import NotificationType
from app.core.utils import utcnow


async def notify(
    db: AsyncIOMotorDatabase,
    *,
    user_ids: List[str],
    type: NotificationType,
    title: str,
    body: str = "",
    data: Optional[Dict[str, Any]] = None,
) -> None:
    if not user_ids:
        return
    now = utcnow()
    await db.notifications.insert_many([
        {
            "user_id": uid,
            "type": type.value,
            "title": title,
            "body": body,
            "data": data or {},
            "read": False,
            "created_at": now,
        }
        for uid in user_ids if uid
    ])


async def log_activity(
    db: AsyncIOMotorDatabase,
    *,
    actor_id: Optional[str],
    actor_name: str,
    action: str,
    subject: str,
    case_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    await db.activities.insert_one({
        "actor_id": actor_id,
        "actor_name": actor_name,
        "action": action,
        "subject": subject,
        "case_id": case_id,
        "request_id": request_id,
        "created_at": utcnow(),
    })
