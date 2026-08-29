"""admin/PlatformSettings.tsx — feature flags, limits, maintenance mode.  [INFERRED]"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.deps import CurrentUser, require_super_admin
from app.core.deps import language as request_language
from app.core.i18n import translate
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
    },
    "clients_can_register_self": {
        "type": "bool",
        "default": True,
        "section": "access_registration",
    },
    "partners_join_by_invitation_only": {
        "type": "bool",
        "default": True,
        "section": "access_registration",
    },
    "auto_approve_new_organizations": {
        "type": "bool",
        "default": False,
        "section": "access_registration",
    },
    "ai_assistant_enabled": {
        "type": "bool",
        "default": True,
        "section": "features_operations",
    },
    "document_checks_enabled": {
        "type": "bool",
        "default": True,
        "section": "features_operations",
    },
    "maintenance_mode": {
        "type": "bool",
        "default": False,
        "section": "features_operations",
    },
    "max_document_size_mb": {
        "type": "int",
        "default": 10,
        "min": 1,
        "section": "limits_contact",
    },
    "document_retention_months": {
        "type": "int",
        "default": 24,
        "min": 1,
        "section": "limits_contact",
    },
    "support_email": {
        "type": "string",
        "default": "support@webimove.com",
        "section": "limits_contact",
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
        "section": "limits_contact",
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
        "settings": [
            "consultants_can_create_organization",
            "clients_can_register_self",
            "partners_join_by_invitation_only",
            "auto_approve_new_organizations",
        ],
    },
    {
        "key": "features_operations",
        "settings": [
            "ai_assistant_enabled",
            "maintenance_mode",
        ],
    },
    {
        "key": "limits_contact",
        "settings": [
            "max_document_size_mb",
            "document_retention_months",
            "trial_days",
            "support_email",
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
    # No label or description here: those live in the catalogue and are filled
    # in by the endpoint, which knows the caller's language. Keeping an English
    # copy alongside would be a second source of truth that silently loses.
    out = {"key": key, "value": value}
    if spec.get("type"):
        out["type"] = spec["type"]
    if spec.get("section"):
        out["section"] = spec["section"]
    # The client needs the bounds to validate before it posts.
    for bound in ("min", "max"):
        if bound in spec:
            out[bound] = spec[bound]
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
async def get_settings(user: CurrentUser = Depends(require_super_admin),
                       lang: str = Depends(request_language)):
    stored: Dict[str, Any] = {}
    async for row in platform_db().platform_settings.find():
        key = _canonical_key(row["key"])
        stored[key] = row["value"]

    settings = _merge_aliases({**DEFAULTS, **stored})
    sections = []
    for section in SECTION_DEFINITIONS:
        items = []
        for key in section["settings"]:
            meta = _setting_meta(key, settings.get(key, DEFAULTS.get(key)))
            # The English in SETTING_SPECS is the fallback; the catalogue is
            # what an operator actually reads, in their own language.
            meta["label"] = translate(f"setting.{key}", lang)
            meta["description"] = translate(f"setting.{key}.description", lang)
            items.append(meta)
        sections.append({
            "key": section["key"],
            "title": translate(f"settings_section.{section['key']}", lang),
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
