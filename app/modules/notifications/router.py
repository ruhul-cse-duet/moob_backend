"""The notification feed, and the devices it is pushed to.

The feed endpoints read the tenant database and so are for workspace accounts;
the device and preference endpoints deliberately do not, because a platform
administrator carries no tenant and still needs their phone to ring. Both kinds
of account therefore register at the same path.
"""
from typing import List, Optional

from fastapi import APIRouter, Body, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, get_current_user, get_tenant_db, page_params
from app.core.enums import NotificationType
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import Message, PageParams
from app.services import push
from app.services.pagination import paginate

router = APIRouter(prefix="/notifications", tags=["Notifications"])


class DeviceRegister(BaseModel):
    """One handset asking to be pushed to.

    `token` is the FCM registration token the app gets from Firebase. It rotates
    - on reinstall, on restore to a new phone, occasionally on its own - so the
    app re-registers on every launch and this endpoint is an upsert rather than
    a create.
    """
    token: str = Field(min_length=8, max_length=4096)
    platform: str = Field(default="unknown",
                          description="android | ios | web | macos | windows | linux")
    device_id: Optional[str] = Field(default=None, max_length=200)
    device_name: Optional[str] = Field(default=None, max_length=200)
    app_version: Optional[str] = Field(default=None, max_length=50)
    locale: Optional[str] = Field(default=None, max_length=20)


class DeviceUnregister(BaseModel):
    token: str = Field(min_length=8, max_length=4096)


class PushPreferences(BaseModel):
    """Push can be silenced wholesale, or per kind of update.

    Absent fields are left as they were, so the app can flip one switch without
    having to send the whole object back.
    """
    enabled: Optional[bool] = None
    muted_types: Optional[List[str]] = Field(
        default=None,
        description="Notification types to stay silent for; the feed still records them.",
    )


# --------------------------------------------------------------------------- #
# Feed
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Devices
#
# Declared before /{notification_id}/read so a literal path is never mistaken
# for an id, and dependent only on `get_current_user` so platform staff - who
# have no tenant database - can register too.
# --------------------------------------------------------------------------- #


@router.post("/devices", summary="Register this device for push notifications")
async def register_device(payload: DeviceRegister,
                          user: CurrentUser = Depends(get_current_user)):
    device = await push.register_device(
        user_id=user.id,
        token=payload.token,
        platform=payload.platform,
        device_id=payload.device_id,
        device_name=payload.device_name,
        app_version=payload.app_version,
        locale=payload.locale,
        tenant_id=user.tenant_id,
        role=user.role.value,
    )
    return {
        "success": True,
        "message": "Device registered for push notifications",
        "detail": "Device registered for push notifications",
        # So the app can tell "we have your token" from "and we can actually
        # send to it", which are different problems to debug.
        "push_configured": push.is_configured(),
        "device": serialize(device),
    }


@router.post("/devices/unregister", response_model=Message,
             summary="Stop pushing to this device")
async def unregister_device(payload: DeviceUnregister,
                            user: CurrentUser = Depends(get_current_user)):
    removed = await push.unregister_device(user_id=user.id, token=payload.token)
    return {
        "success": True,
        "message": "Device unregistered" if removed else "Device was not registered",
        "detail": "Device unregistered" if removed else "Device was not registered",
    }


@router.get("/devices", summary="Devices receiving push for this account")
async def list_devices(user: CurrentUser = Depends(get_current_user)):
    devices = await push.list_devices(user_id=user.id)
    return {
        "success": True,
        "message": "OK",
        "push_configured": push.is_configured(),
        "items": [serialize(d) for d in devices],
        "total": len(devices),
    }


@router.delete("/devices", response_model=Message,
               summary="Stop pushing to every device on this account")
async def unregister_all_devices(user: CurrentUser = Depends(get_current_user)):
    count = await push.unregister_all(user_id=user.id)
    return {
        "success": True,
        "message": f"{count} device(s) unregistered",
        "detail": f"{count} device(s) unregistered",
    }


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


@router.get("/preferences", summary="Push preferences for this account")
async def get_preferences(user: CurrentUser = Depends(get_current_user)):
    prefs = await push.get_preferences(user.id)
    return {
        "success": True,
        "message": "OK",
        "push_configured": push.is_configured(),
        # The full list, so the app can build the mute switches without
        # hardcoding a copy of the enum that then drifts.
        "available_types": [t.value for t in NotificationType],
        **prefs,
    }


@router.put("/preferences", summary="Update push preferences")
async def update_preferences(payload: PushPreferences,
                             user: CurrentUser = Depends(get_current_user)):
    prefs = await push.set_preferences(
        user.id, enabled=payload.enabled, muted_types=payload.muted_types)
    return {
        "success": True,
        "message": "Push preferences updated",
        "detail": "Push preferences updated",
        **prefs,
    }


@router.post("/devices/test", summary="Send a test push to this account")
async def test_push(title: str = Body(default="Test notification", embed=True),
                    body: str = Body(default="Push notifications are working.",
                                     embed=True),
                    user: CurrentUser = Depends(get_current_user)):
    """Sends to this account's own devices, and waits for the result.

    Deliberately synchronous, unlike every other push in the system: the point
    of a test is the tally that comes back, so `send_to_users` is awaited rather
    than dispatched. Reads as a success with zero devices when nothing is
    registered, which is the honest answer.
    """
    result = await push.send_to_users(
        [user.id],
        title=title,
        body=body,
        data={"test": True},
        type="system",
    )
    return {
        "success": True,
        "message": f"{result['sent']} of {result['devices']} device(s) reached",
        "detail": f"{result['sent']} of {result['devices']} device(s) reached",
        "push_configured": push.is_configured(),
        **result,
    }


# --------------------------------------------------------------------------- #
# Read state
# --------------------------------------------------------------------------- #


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
