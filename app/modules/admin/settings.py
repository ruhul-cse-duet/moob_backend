"""admin/PlatformSettings.tsx — feature flags, limits, maintenance mode.  [INFERRED]"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import AuditAction
from app.core.exceptions import BadRequest
from app.core.utils import utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message
from app.services import audit

router = APIRouter(prefix="/settings", tags=["Super Admin · Platform settings"])

SETTING_SPECS: Dict[str, Dict[str, Any]] = {
    "consultants_can_create_organization": {
        "type": "bool",
        "default": True,
        "section": "access_registration",
        "label": "Consultants can create their own organization",
        "description": "Turning this off makes every new organization admin-created.",
    },
    "clients_can_register_self": {
        "type": "bool",
        "default": True,
        "section": "access_registration",
        "label": "Clients can register themselves",
        "description": "Clients choose a consultant from the directory during sign-up.",
    },
    "partners_join_by_invitation_only": {
        "type": "bool",
        "default": True,
        "section": "access_registration",
        "label": "Partners join by invitation only",
        "description": "Enforced by the platform — partners never self-register.",
    },
    "auto_approve_new_organizations": {
        "type": "bool",
        "default": False,
        "section": "access_registration",
        "label": "Auto-approve new organizations",
        "description": "Skips the manual verification step in the approval queue.",
    },
    "ai_assistant_enabled": {
        "type": "bool",
        "default": True,
        "section": "features_operations",
        "label": "AI assistant and document checks",
        "description": "Powers OCR, extracted fields and the assistant in all three apps.",
    },
    "document_checks_enabled": {
        "type": "bool",
        "default": True,
        "section": "features_operations",
        "label": "Document checks",
        "description": "Runs document OCR and automated review on uploads.",
    },
    "maintenance_mode": {
        "type": "bool",
        "default": False,
        "section": "features_operations",
        "label": "Maintenance mode",
        "description": "Makes every workspace read-only during a deployment window.",
    },
    "max_document_size_mb": {
        "type": "int",
        "default": 10,
        "min": 1,
        "section": "limits_contact",
        "label": "Maximum document size (MB)",
        "description": "Uploaded files larger than this are rejected.",
    },
    "document_retention_months": {
        "type": "int",
        "default": 24,
        "min": 1,
        "section": "limits_contact",
        "label": "Document retention after case closure (months)",
        "description": "Controls how long closed-case files stay available.",
    },
    "support_email": {
        "type": "string",
        "default": "support@webimove.com",
        "section": "limits_contact",
        "label": "Support email",
        "description": "Recipient address for support and platform notifications.",
    },
    "maintenance_message": {
        "type": "string",
        "default": "",
        "allow_empty": True,
    },
    "trial_days": {
        "type": "int",
        "default": 0,
        "min": 0,
    },
    "otp_expire_minutes": {
        "type": "int",
        "default": 10,
        "min": 1,
    },
    "default_plan_code": {
        "type": "string",
        "default": "professional",
        "allow_empty": False,
    },
}

SETTING_ALIASES = {
    "signups_open": "clients_can_register_self",
    "ai_document_analysis_enabled": "document_checks_enabled",
    "max_upload_mb": "max_document_size_mb",
}

SECTION_DEFINITIONS = [
    {
        "key": "access_registration",
        "title": "Access & registration",
        "settings": [
            "consultants_can_create_organization",
            "clients_can_register_self",
            "partners_join_by_invitation_only",
            "auto_approve_new_organizations",
        ],
    },
    {
        "key": "features_operations",
        "title": "Features & operations",
        "settings": [
            "ai_assistant_enabled",
            "maintenance_mode",
        ],
    },
    {
        "key": "limits_contact",
        "title": "Limits & contact",
        "settings": [
            "max_document_size_mb",
            "document_retention_months",
        ],
    },
]

DEFAULTS: Dict[str, Any] = {
    key: spec["default"]
    for key, spec in SETTING_SPECS.items()
    if "default" in spec
}


class SettingUpdate(BaseModel):
    value: Any


def _canonical_key(key: str) -> str:
    return SETTING_ALIASES.get(key, key)


def _coerce_value(key: str, value: Any) -> Any:
    spec = SETTING_SPECS.get(key)
    if not spec:
        return value

    value_type = spec["type"]
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        raise BadRequest(f"{key} must be a boolean")

    if value_type == "int":
        if isinstance(value, bool):
            raise BadRequest(f"{key} must be an integer")
        if isinstance(value, int):
            coerced = value
        elif isinstance(value, str) and value.strip():
            try:
                coerced = int(value.strip())
            except ValueError as exc:
                raise BadRequest(f"{key} must be an integer") from exc
        else:
            raise BadRequest(f"{key} must be an integer")
        minimum = spec.get("min")
        if minimum is not None and coerced < minimum:
            raise BadRequest(f"{key} must be at least {minimum}")
        return coerced

    if value_type == "string":
        if value is None:
            raise BadRequest(f"{key} must be a string")
        if not isinstance(value, str):
            raise BadRequest(f"{key} must be a string")
        cleaned = value.strip()
        if not cleaned and not spec.get("allow_empty", False):
            raise BadRequest(f"{key} cannot be empty")
        return cleaned

    return value


def _setting_meta(key: str, value: Any) -> Dict[str, Any]:
    spec = SETTING_SPECS.get(key, {})
    out = {"key": key, "value": value}
    if spec.get("label"):
        out["label"] = spec["label"]
    if spec.get("description"):
        out["description"] = spec["description"]
    if spec.get("type"):
        out["type"] = spec["type"]
    if spec.get("section"):
        out["section"] = spec["section"]
    return out


def _merge_aliases(settings: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(settings)
    for alias, canonical in SETTING_ALIASES.items():
        if canonical in out:
            out[alias] = out[canonical]
        elif alias in out:
            out[canonical] = out[alias]
    return out


@router.get("", summary="All platform settings (grouped UI sections and defaults)")
async def get_settings(user: CurrentUser = Depends(require_super_admin)):
    stored: Dict[str, Any] = {}
    async for row in platform_db().platform_settings.find():
        key = _canonical_key(row["key"])
        stored[key] = row["value"]

    settings = _merge_aliases({**DEFAULTS, **stored})
    sections = []
    for section in SECTION_DEFINITIONS:
        items = [
            _setting_meta(key, settings.get(key, DEFAULTS.get(key)))
            for key in section["settings"]
        ]
        sections.append({
            "key": section["key"],
            "title": section["title"],
            "settings": items,
        })

    return {
        "settings": settings,
        "defaults": _merge_aliases(DEFAULTS),
        "sections": sections,
    }


@router.put("/{key}", summary="Set one platform setting")
async def set_setting(key: str, payload: SettingUpdate,
                      user: CurrentUser = Depends(require_super_admin)):
    canonical_key = _canonical_key(key)
    value = _coerce_value(canonical_key, payload.value)

    await platform_db().platform_settings.update_one(
        {"key": canonical_key},
        {"$set": {"value": value, "updated_by": user.id, "updated_at": utcnow()}},
        upsert=True,
    )
    if canonical_key != key:
        await platform_db().platform_settings.delete_one({"key": key})

    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject=f"setting:{canonical_key}",
                       meta={"value": value})
    return {"key": canonical_key, "value": value}


@router.delete("/{key}", response_model=Message, summary="Reset a setting to its default")
async def reset_setting(key: str, user: CurrentUser = Depends(require_super_admin)):
    canonical_key = _canonical_key(key)
    await platform_db().platform_settings.delete_one({"key": canonical_key})
    if canonical_key != key:
        await platform_db().platform_settings.delete_one({"key": key})
    return {"detail": f"'{canonical_key}' reset to default"}


async def get_setting(key: str, default: Any = None) -> Any:
    """Helper for the rest of the app to read a live platform setting."""
    canonical_key = _canonical_key(key)
    row = await platform_db().platform_settings.find_one({"key": canonical_key})
    if row is None and canonical_key != key:
        row = await platform_db().platform_settings.find_one({"key": key})
    if row is not None:
        return row["value"]
    if canonical_key in DEFAULTS:
        return DEFAULTS[canonical_key]
    if key in DEFAULTS:
        return DEFAULTS[key]
    return default
