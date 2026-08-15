"""admin/Organizations.tsx + OrganizationDetail — Super Admin Organizations UI."""
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Query

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import (
    ORG_LIST_TAB_STATUSES,
    AuditAction,
    BillingCycle,
    PlanCode,
    Role,
    TenantStatus,
)
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db, tenant_db
from app.modules.admin.billing import _monthly_value
from app.modules.subscriptions.plans import PLANS, plan_by_code
from app.schemas.common import Message, PageParams
from app.services import audit
from app.services.pagination import paginate

router = APIRouter(prefix="/organizations", tags=["Super Admin · Organizations"])

OrgTab = Literal["all", "approval", "active", "suspended", "expired"]


def _location(tenant: dict) -> Optional[str]:
    parts = [p for p in [tenant.get("city"), tenant.get("country")] if p]
    return ", ".join(parts) if parts else None


def _status_label(status: str) -> str:
    return {
        TenantStatus.AWAITING_APPROVAL.value: "Awaiting approval",
        TenantStatus.PENDING_VERIFICATION.value: "Pending verification",
        TenantStatus.PENDING_PAYMENT.value: "Pending payment",
        TenantStatus.ACTIVE.value: "Active",
        TenantStatus.SUSPENDED.value: "Suspended",
        TenantStatus.EXPIRED.value: "Expired",
        TenantStatus.PAST_DUE.value: "Past due",
        TenantStatus.CANCELLED.value: "Cancelled",
    }.get(status, status.replace("_", " ").title())


def _signed_up_label(dt) -> Optional[str]:
    if not dt:
        return None
    try:
        days = (utcnow() - dt).days
    except TypeError:
        return None
    if days <= 0:
        return "Signed up Today"
    if days == 1:
        return "Signed up Yesterday"
    if days < 14:
        return f"Signed up {days} days ago"
    return f"Signed up {dt.strftime('%d %b %Y')}"


async def _enrich_org_card(item: dict) -> dict:
    """Shape one organization card for the Organizations list UI."""
    tid = item["id"]
    tdb = tenant_db(tid)
    consultants = await tdb.users.count_documents(
        {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}})
    partners = await tdb.users.count_documents({"role": Role.PARTNER.value})
    clients = await tdb.users.count_documents({"role": Role.CLIENT.value})

    sub = await platform_db().subscriptions.find_one(
        {"tenant_id": tid, "status": "active"})
    mrr = round(_monthly_value(sub), 2) if sub else 0.0

    status = item.get("status")
    plan_code = item.get("plan_code")
    plan_name = None
    if plan_code:
        try:
            plan_name = PLANS[PlanCode(plan_code)]["name"]
        except (KeyError, ValueError):
            plan_name = plan_code

    verified = bool(item.get("verified")) or status == TenantStatus.ACTIVE.value
    signed_up_at = item.get("signed_up_at") or item.get("created_at")

    item.update({
        "owner_name": item.get("owner_name"),
        "owner_email": item.get("owner_email"),
        "location": _location(item),
        "status_label": _status_label(status) if status else None,
        "plan_name": plan_name,
        "verified": verified,
        "verification_label": "Verified" if verified else "Unverified",
        "consultants": consultants,
        "partners": partners,
        "clients": clients,
        "mrr": mrr,
        "currency": "USD",
        "can_approve": status == TenantStatus.AWAITING_APPROVAL.value,
        "signed_up_at": signed_up_at,
        "signed_up_label": _signed_up_label(signed_up_at),
        # Back-compat fields used by older clients
        "seats_used": consultants,
        "active_cases": await tdb.cases.count_documents({"stage": {"$ne": "completed"}}),
    })
    return item


@router.get("/tabs", summary="Organization list tab counts (All / Approval / …)")
async def organization_tabs(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    counts = {
        "all": await db.tenants.count_documents({}),
        "approval": await db.tenants.count_documents({
            "status": {"$in": [s.value for s in ORG_LIST_TAB_STATUSES["approval"]]}}),
        "active": await db.tenants.count_documents({
            "status": TenantStatus.ACTIVE.value}),
        "suspended": await db.tenants.count_documents({
            "status": TenantStatus.SUSPENDED.value}),
        "expired": await db.tenants.count_documents({
            "status": {"$in": [s.value for s in ORG_LIST_TAB_STATUSES["expired"]]}}),
    }
    return {"success": True, "message": "OK", "tabs": counts}


@router.get("", summary="Organizations list — search, status tabs, card metrics")
async def list_organizations(
    tab: OrgTab = Query("all", description="all | approval | active | suspended | expired"),
    status: Optional[TenantStatus] = Query(None, description="Exact status override"),
    plan_code: Optional[PlanCode] = Query(None),
    search: Optional[str] = Query(
        None, description="Search organization, owner or country"),
    params: PageParams = Depends(page_params),
    user: CurrentUser = Depends(require_super_admin),
):
    query: dict = {}
    if status:
        query["status"] = status.value
    elif tab != "all":
        statuses = ORG_LIST_TAB_STATUSES.get(tab)
        if not statuses:
            raise BadRequest("Unknown organizations tab")
        query["status"] = {"$in": [s.value for s in statuses]}
    if plan_code:
        query["plan_code"] = plan_code.value
    if search:
        rx = {"$regex": search, "$options": "i"}
        query["$or"] = [
            {"name": rx},
            {"owner_name": rx},
            {"owner_email": rx},
            {"country": rx},
            {"city": rx},
        ]

    page = await paginate(platform_db(), "tenants", query, params,
                          sort=[("created_at", -1)])
    page["items"] = [await _enrich_org_card(item) for item in page["items"]]
    page["tab"] = tab
    return page


@router.get("/{tenant_id}", summary="Organization detail with usage and health")
async def organization_detail(tenant_id: str,
                              user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(tenant_id)})
    if not tenant:
        raise NotFound("Organization not found")
    tdb = tenant_db(tenant_id)
    plan = None
    if tenant.get("plan_code"):
        try:
            plan = await plan_by_code(PlanCode(tenant["plan_code"]))
        except ValueError:
            plan = None
    since = utcnow() - timedelta(days=30)
    card = await _enrich_org_card(serialize(tenant))
    
    owner = await tdb.users.find_one({"role": Role.CONSULTANT_OWNER.value})
    if owner:
        card["owner_phone"] = owner.get("mobile")

    return {
        "success": True,
        "message": "OK",
        "organization": card,
        "plan": plan,
        "subscription": serialize(
            await db.subscriptions.find_one(
                {"tenant_id": tenant_id}, sort=[("created_at", -1)])),
        "usage": {
            "consultants": card["consultants"],
            "partners": card["partners"],
            "clients": card["clients"],
            "requests": await tdb.requests.count_documents({}),
            "cases_total": await tdb.cases.count_documents({}),
            "cases_active": card["active_cases"],
            "documents": await tdb.documents.count_documents({}),
            "storage_bytes": await _storage_bytes(tdb),
            "mrr": card["mrr"],
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


@router.post("/{tenant_id}/approve", response_model=Message,
             summary="Approve an organization awaiting platform verification")
async def approve_organization(tenant_id: str,
                               user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    tenant = await db.tenants.find_one({"_id": oid(tenant_id)})
    if not tenant:
        raise NotFound("Organization not found")
    if tenant.get("status") == TenantStatus.ACTIVE.value and tenant.get("verified"):
        raise Conflict("Organization is already approved")
    if tenant.get("status") not in {
        TenantStatus.AWAITING_APPROVAL.value,
        TenantStatus.PENDING_VERIFICATION.value,
        TenantStatus.PENDING_PAYMENT.value,
        TenantStatus.SUSPENDED.value,
    }:
        # Allow re-approve from suspended → active; block expired/cancelled without status API
        if tenant.get("status") in {TenantStatus.EXPIRED.value, TenantStatus.CANCELLED.value}:
            raise BadRequest(
                "Reactivate expired/cancelled organizations via the status endpoint"
            )

    now = utcnow()
    await db.tenants.update_one(
        {"_id": oid(tenant_id)},
        {"$set": {
            "status": TenantStatus.ACTIVE.value,
            "verified": True,
            "activated_at": tenant.get("activated_at") or now,
            "approved_at": now,
            "approved_by": user.id,
            "status_reason": None,
            "updated_at": now,
        }},
    )
    await audit.record(
        action=AuditAction.TENANT_STATUS_CHANGED,
        actor_id=user.id,
        actor_email=user.email,
        actor_role="super_admin",
        tenant_id=tenant_id,
        subject=TenantStatus.ACTIVE.value,
        detail="Approved by platform administrator",
        meta={"previous_status": tenant.get("status")},
    )
    return {
        "success": True,
        "message": f"{tenant.get('name')} has been approved",
        "detail": f"{tenant.get('name')} has been approved",
    }


@router.post("/{tenant_id}/status", response_model=Message,
             summary="Suspend, reactivate, expire or cancel an organization")
async def set_status(tenant_id: str, status: TenantStatus = Body(embed=True),
                     reason: Optional[str] = Body(None, embed=True),
                     user: CurrentUser = Depends(require_super_admin)):
    if status == TenantStatus.AWAITING_APPROVAL:
        raise BadRequest("Use the Approve action to move an organization to Active")
    data = {
        "status": status.value,
        "status_reason": reason,
        "updated_at": utcnow(),
    }
    if status == TenantStatus.ACTIVE:
        data["verified"] = True
        data["activated_at"] = utcnow()
    if status == TenantStatus.SUSPENDED:
        data["suspended_at"] = utcnow()
    result = await platform_db().tenants.update_one({"_id": oid(tenant_id)}, {"$set": data})
    if not result.matched_count:
        raise NotFound("Organization not found")
    await audit.record(action=AuditAction.TENANT_STATUS_CHANGED, actor_id=user.id,
                       actor_email=user.email, actor_role="super_admin",
                       tenant_id=tenant_id, subject=status.value, detail=reason)
    return {
        "success": True,
        "message": f"Organization set to {status.value}",
        "detail": f"Organization set to {status.value}",
    }


@router.post("/{tenant_id}/plan", response_model=Message,
             summary="Override an organization's plan (comps, migrations, enterprise deals)")
async def override_plan(tenant_id: str, plan_code: PlanCode = Body(embed=True),
                        billing_cycle: BillingCycle = Body(embed=True),
                        note: Optional[str] = Body(None, embed=True),
                        user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    result = await db.tenants.update_one(
        {"_id": oid(tenant_id)},
        {"$set": {"plan_code": plan_code.value, "plan_override_note": note,
                  "updated_at": utcnow()}})
    if not result.matched_count:
        raise NotFound("Organization not found")
        
    # Also update the active subscription's plan and billing cycle
    await db.subscriptions.update_one(
        {"tenant_id": tenant_id, "status": "active"},
        {"$set": {
            "plan_code": plan_code.value, 
            "billing_cycle": billing_cycle.value,
            "updated_at": utcnow()
        }}
    )
    
    await audit.record(action=AuditAction.PLAN_CHANGED, actor_id=user.id,
                       actor_email=user.email, tenant_id=tenant_id,
                       subject=f"{plan_code.value} ({billing_cycle.value})", detail=note)
    return {
        "success": True,
        "message": f"Plan set to {plan_code.value} ({billing_cycle.value})",
        "detail": f"Plan set to {plan_code.value} ({billing_cycle.value})",
    }
