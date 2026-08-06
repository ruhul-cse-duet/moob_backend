"""admin/Organizations.tsx + admin/OrganizationDetail.tsx  [INFERRED]"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import AuditAction, PlanCode, Role, TenantStatus
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db, tenant_db
from app.modules.subscriptions.plans import PLANS
from app.schemas.common import Message, PageParams
from app.services import audit
from app.services.pagination import paginate

router = APIRouter(prefix="/organizations", tags=["Super Admin · Organizations"])


@router.get("", summary="All organizations on the platform")
async def list_organizations(status: Optional[TenantStatus] = Query(None),
                             plan_code: Optional[PlanCode] = Query(None),
                             search: Optional[str] = Query(None),
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(require_super_admin)):
    query = {}
    if status:
        query["status"] = status.value
    if plan_code:
        query["plan_code"] = plan_code.value
    if search:
        query["$or"] = [{"name": {"$regex": search, "$options": "i"}},
                        {"owner_email": {"$regex": search, "$options": "i"}},
                        {"country": {"$regex": search, "$options": "i"}}]
    page = await paginate(platform_db(), "tenants", query, params, sort=[("created_at", -1)])
    for item in page["items"]:
        tdb = tenant_db(item["id"])
        item["seats_used"] = await tdb.users.count_documents(
            {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}})
        item["active_cases"] = await tdb.cases.count_documents({"stage": {"$ne": "completed"}})
    return page


@router.get("/{tenant_id}", summary="Organization detail with usage and health")
async def organization_detail(tenant_id: str,
                              user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(tenant_id)})
    if not tenant:
        raise NotFound("Organization not found")
    tdb = tenant_db(tenant_id)
    plan = PLANS.get(PlanCode(tenant["plan_code"])) if tenant.get("plan_code") else None
    since = utcnow() - timedelta(days=30)

    return {
        "organization": serialize(tenant),
        "plan": plan,
        "subscription": serialize(
            await db.subscriptions.find_one({"tenant_id": tenant_id, "status": "active"})),
        "usage": {
            "consultants": await tdb.users.count_documents(
                {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}}),
            "partners": await tdb.users.count_documents({"role": Role.PARTNER.value}),
            "clients": await tdb.users.count_documents({"role": Role.CLIENT.value}),
            "requests": await tdb.requests.count_documents({}),
            "cases_total": await tdb.cases.count_documents({}),
            "cases_active": await tdb.cases.count_documents({"stage": {"$ne": "completed"}}),
            "documents": await tdb.documents.count_documents({}),
            "storage_bytes": await _storage_bytes(tdb),
        },
        "activity_30d": {
            "requests": await tdb.requests.count_documents({"created_at": {"$gte": since}}),
            "cases": await tdb.cases.count_documents({"created_at": {"$gte": since}}),
            "logins": await db.audit_log.count_documents(
                {"tenant_id": tenant_id, "action": AuditAction.LOGIN.value,
                 "created_at": {"$gte": since}}),
        },
        "team": [serialize({k: v for k, v in u.items() if k != "password_hash"})
                 async for u in tdb.users.find(
                     {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}})],
        "open_tickets": await db.support_tickets.count_documents(
            {"tenant_id": tenant_id, "status": {"$nin": ["resolved", "closed"]}}),
    }


async def _storage_bytes(tdb) -> int:
    total = 0
    for bucket in ("documents.files", "deliverables.files"):
        cursor = tdb[bucket].aggregate([{"$group": {"_id": None, "n": {"$sum": "$length"}}}])
        async for row in cursor:
            total += row.get("n", 0)
    return total


@router.post("/{tenant_id}/status", response_model=Message,
             summary="Suspend, reactivate or cancel an organization")
async def set_status(tenant_id: str, status: TenantStatus = Body(embed=True),
                     reason: Optional[str] = Body(None, embed=True),
                     user: CurrentUser = Depends(require_super_admin)):
    result = await platform_db().tenants.update_one(
        {"_id": oid(tenant_id)},
        {"$set": {"status": status.value, "status_reason": reason, "updated_at": utcnow()}})
    if not result.matched_count:
        raise NotFound("Organization not found")
    await audit.record(action=AuditAction.TENANT_STATUS_CHANGED, actor_id=user.id,
                       actor_email=user.email, actor_role="super_admin",
                       tenant_id=tenant_id, subject=status.value, detail=reason)
    return {"detail": f"Organization set to {status.value}"}


@router.post("/{tenant_id}/plan", response_model=Message,
             summary="Override an organization's plan (comps, migrations, enterprise deals)")
async def override_plan(tenant_id: str, plan_code: PlanCode = Body(embed=True),
                        note: Optional[str] = Body(None, embed=True),
                        user: CurrentUser = Depends(require_super_admin)):
    result = await platform_db().tenants.update_one(
        {"_id": oid(tenant_id)},
        {"$set": {"plan_code": plan_code.value, "plan_override_note": note,
                  "updated_at": utcnow()}})
    if not result.matched_count:
        raise NotFound("Organization not found")
    await audit.record(action=AuditAction.PLAN_CHANGED, actor_id=user.id,
                       actor_email=user.email, tenant_id=tenant_id,
                       subject=plan_code.value, detail=note)
    return {"detail": f"Plan set to {plan_code.value}"}
