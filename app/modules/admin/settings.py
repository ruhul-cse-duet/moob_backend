"""admin/PlatformSettings.tsx — feature flags, limits, maintenance mode.  [INFERRED]"""
from typing import Any, Dict

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import AuditAction
from app.core.utils import utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message
from app.services import audit

router = APIRouter(prefix="/settings", tags=["Super Admin · Platform settings"])

DEFAULTS: Dict[str, Any] = {
    "maintenance_mode": False,
    "maintenance_message": "",
    "signups_open": True,
    "trial_days": 0,
    "ai_document_analysis_enabled": True,
    "ai_assistant_enabled": True,
    "max_upload_mb": 25,
    "otp_expire_minutes": 10,
    "support_email": "support@webimove.com",
    "default_plan_code": "professional",
}


class SettingUpdate(BaseModel):
    value: Any


@router.get("", summary="All platform settings (defaults merged with overrides)")
async def get_settings(user: CurrentUser = Depends(require_super_admin)):
    stored = {row["key"]: row["value"]
              async for row in platform_db().platform_settings.find()}
    return {"settings": {**DEFAULTS, **stored}, "defaults": DEFAULTS}


@router.put("/{key}", summary="Set one platform setting")
async def set_setting(key: str, payload: SettingUpdate,
                      user: CurrentUser = Depends(require_super_admin)):
    await platform_db().platform_settings.update_one(
        {"key": key},
        {"$set": {"value": payload.value, "updated_by": user.id, "updated_at": utcnow()}},
        upsert=True)
    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject=f"setting:{key}",
                       meta={"value": payload.value})
    return {"key": key, "value": payload.value}


@router.delete("/{key}", response_model=Message, summary="Reset a setting to its default")
async def reset_setting(key: str, user: CurrentUser = Depends(require_super_admin)):
    await platform_db().platform_settings.delete_one({"key": key})
    return {"detail": f"'{key}' reset to default"}


async def get_setting(key: str, default: Any = None) -> Any:
    """Helper for the rest of the app to read a live platform setting."""
    row = await platform_db().platform_settings.find_one({"key": key})
    return row["value"] if row else DEFAULTS.get(key, default)
