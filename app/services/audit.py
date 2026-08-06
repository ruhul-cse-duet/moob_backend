"""Platform-wide audit trail — the data behind admin/Oversight.tsx. [INFERRED]"""
from typing import Any, Dict, Optional

from app.core.enums import AuditAction
from app.core.utils import utcnow
from app.db.mongo import platform_db


async def record(
    *,
    action: AuditAction,
    actor_id: Optional[str] = None,
    actor_email: Optional[str] = None,
    actor_role: Optional[str] = None,
    tenant_id: Optional[str] = None,
    subject: Optional[str] = None,
    detail: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    await platform_db().audit_log.insert_one({
        "action": action.value,
        "actor_id": actor_id,
        "actor_email": actor_email,
        "actor_role": actor_role,
        "tenant_id": tenant_id,
        "subject": subject,
        "detail": detail,
        "ip": ip,
        "user_agent": user_agent,
        "meta": meta or {},
        "created_at": utcnow(),
    })
