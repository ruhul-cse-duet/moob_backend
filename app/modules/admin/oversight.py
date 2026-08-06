"""admin/Oversight.tsx — audit trail, anomaly flags, platform health.  [INFERRED]"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import AuditAction, DataRequestStatus, Role, TenantStatus
from app.core.utils import serialize, utcnow
from app.db.mongo import get_client, platform_db, tenant_db
from app.schemas.common import PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/oversight", tags=["Super Admin · Oversight"])


@router.get("/audit-log", summary="Platform audit trail")
async def audit_log(action: Optional[AuditAction] = Query(None),
                    tenant_id: Optional[str] = Query(None),
                    actor_email: Optional[str] = Query(None),
                    days: int = Query(30, ge=1, le=365),
                    params: PageParams = Depends(page_params),
                    user: CurrentUser = Depends(require_super_admin)):
    query = {"created_at": {"$gte": utcnow() - timedelta(days=days)}}
    if action:
        query["action"] = action.value
    if tenant_id:
        query["tenant_id"] = tenant_id
    if actor_email:
        query["actor_email"] = {"$regex": actor_email, "$options": "i"}
    return await paginate(platform_db(), "audit_log", query, params,
                          sort=[("created_at", -1)])


@router.get("/health", summary="Platform health")
async def health(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    try:
        info = await get_client().admin.command("serverStatus")
        db_ok, uptime = True, info.get("uptime")
    except Exception:  # noqa: BLE001
        db_ok, uptime = False, None
    return {
        "database_reachable": db_ok,
        "database_uptime_seconds": uptime,
        "tenant_databases": await db.tenants.count_documents({}),
        "open_tickets": await db.support_tickets.count_documents(
            {"status": {"$nin": ["resolved", "closed"]}}),
        "failed_logins_24h": await db.audit_log.count_documents(
            {"action": AuditAction.LOGIN_FAILED.value,
             "created_at": {"$gte": utcnow() - timedelta(hours=24)}}),
    }


@router.get("/flags", summary="Anomalies worth a human look")
async def flags(user: CurrentUser = Depends(require_super_admin)):
    """Heuristics, not machine learning. Tune the thresholds once you have real traffic."""
    db = platform_db()
    now = utcnow()
    out = []

    # Repeated failed logins from one email
    cursor = db.audit_log.aggregate([
        {"$match": {"action": AuditAction.LOGIN_FAILED.value,
                    "created_at": {"$gte": now - timedelta(hours=24)}}},
        {"$group": {"_id": "$actor_email", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gte": 5}}},
        {"$sort": {"n": -1}}, {"$limit": 20},
    ])
    async for row in cursor:
        out.append({"kind": "repeated_failed_logins", "severity": "high",
                    "subject": row["_id"], "count": row["n"]})

    # Tenants that paid but never onboarded anyone
    async for t in db.tenants.find({"status": TenantStatus.ACTIVE.value,
                                    "activated_at": {"$lte": now - timedelta(days=14)}}):
        tid = str(t["_id"])
        clients = await tenant_db(tid).users.count_documents({"role": Role.CLIENT.value})
        if clients == 0:
            out.append({"kind": "activated_but_unused", "severity": "medium",
                        "subject": t["name"], "tenant_id": tid,
                        "detail": "Active for 14+ days with no clients"})

    # Past-due subscriptions
    async for t in db.tenants.find({"status": TenantStatus.PAST_DUE.value}):
        out.append({"kind": "past_due", "severity": "high", "subject": t["name"],
                    "tenant_id": str(t["_id"])})

    # Stale GDPR requests still unanswered
    async for t in db.tenants.find({"status": TenantStatus.ACTIVE.value}):
        tid = str(t["_id"])
        stale = await tenant_db(tid).data_requests.count_documents(
            {"status": {"$in": [DataRequestStatus.PENDING.value,
                                DataRequestStatus.IN_PROGRESS.value]},
             "created_at": {"$lte": now - timedelta(days=25)}})
        if stale:
            out.append({"kind": "gdpr_request_overdue", "severity": "high",
                        "subject": t["name"], "tenant_id": tid, "count": stale,
                        "detail": "GDPR allows 30 days to respond"})
    return {"flags": out, "generated_at": now}


@router.get("/tenant/{tenant_id}/activity", summary="Recent activity inside one organization")
async def tenant_activity(tenant_id: str, limit: int = Query(50, ge=1, le=200),
                          user: CurrentUser = Depends(require_super_admin)):
    tdb = tenant_db(tenant_id)
    return {"items": [serialize(a) async for a in
                      tdb.activities.find().sort("created_at", -1).limit(limit)]}
