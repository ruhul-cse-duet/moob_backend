"""
Super Admin · Platform Overview (AdminDashboard).

Matches the Platform control Overview UI:
- metric cards (orgs, MRR/ARR, users, active cases)
- Needs your attention counters + approval banner
- Revenue by plan + role distribution
- Organizations at risk, support queue, audit trail
"""
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import (
    AuditAction,
    DocumentStatus,
    PlanCode,
    Role,
    TenantStatus,
    TicketStatus,
)
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db, tenant_db
from app.modules.admin.billing import _monthly_value
from app.modules.admin.admin_users import router as admin_users_router
from app.modules.admin.consultants import router as consultants_router
from app.modules.admin.announcements import router as announcements_router
from app.modules.admin.billing import router as billing_router
from app.modules.admin.helpdesk import router as helpdesk_router
from app.modules.admin.organizations import router as organizations_router
from app.modules.admin.profile import get_profile as get_admin_profile
from app.modules.admin.profile import router as profile_router
from app.modules.admin.setup import router as setup_router
from app.modules.admin.oversight import router as oversight_router
from app.modules.admin.settings import router as settings_router
from app.modules.admin.notifications import router as notifications_router
from app.modules.subscriptions.plans import PLANS

router = APIRouter(prefix="/admin")


def _audit_severity(action: str) -> str:
    critical = {
        AuditAction.TENANT_STATUS_CHANGED.value,
        AuditAction.DATA_DELETION_REQUESTED.value,
        AuditAction.USER_SUSPENDED.value,
    }
    warning = {
        AuditAction.LOGIN_FAILED.value,
        AuditAction.PLAN_CHANGED.value,
        AuditAction.DATA_EXPORT_REQUESTED.value,
    }
    if action in critical:
        return "critical"
    if action in warning:
        return "warning"
    return "info"


def _relative_time(dt) -> Optional[str]:
    if not dt:
        return None
    try:
        delta = utcnow() - dt
    except TypeError:
        return None
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        m = seconds // 60
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    days = seconds // 86400
    if days == 1:
        return "Yesterday"
    if days < 14:
        return f"{days} days ago"
    return dt.strftime("%d %b %Y")


async def _sum_across_tenants(collection: str, query: Dict[str, Any]) -> int:
    total = 0
    async for t in platform_db().tenants.find({}, {"_id": 1}):
        total += await tenant_db(str(t["_id"]))[collection].count_documents(query)
    return total


@router.get("/dashboard", tags=["Super Admin · Dashboard"],
            summary="Platform Overview — metrics, attention, revenue, queues")
async def dashboard(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    now = utcnow()
    first_name = (user.raw.get("full_name") or user.email or "Admin").split()[0]

    # ---- Revenue ----
    mrr = 0.0
    plan_mrr: Dict[str, float] = {p.value: 0.0 for p in PlanCode}
    plan_org_counts: Dict[str, int] = {p.value: 0 for p in PlanCode}
    collected_to_date = 0.0
    outstanding = 0.0

    async for sub in db.subscriptions.find({}):
        total = float(sub.get("total") or 0)
        status = sub.get("status")
        if status == "active":
            monthly = _monthly_value(sub)
            mrr += monthly
            code = sub.get("plan_code")
            if code in plan_mrr:
                plan_mrr[code] += monthly
            collected_to_date += total
        elif status in {"past_due", "failed", "open"}:
            outstanding += total
        elif status in {"active", "paid"}:
            collected_to_date += total

    # Count orgs per plan (active + awaiting)
    async for t in db.tenants.find({"plan_code": {"$exists": True}}):
        code = t.get("plan_code")
        if code in plan_org_counts:
            plan_org_counts[code] += 1

    # Failed-payment orgs also contribute to outstanding estimate
    past_due_n = await db.tenants.count_documents({"status": TenantStatus.PAST_DUE.value})
    async for t in db.tenants.find({"status": TenantStatus.PAST_DUE.value}):
        sub = await db.subscriptions.find_one(
            {"tenant_id": str(t["_id"])}, sort=[("created_at", -1)])
        if sub:
            outstanding += float(sub.get("total") or sub.get("amount") or 0)

    orgs_total = await db.tenants.count_documents({})
    orgs_active = await db.tenants.count_documents({"status": TenantStatus.ACTIVE.value})
    awaiting_approval = await db.tenants.count_documents(
        {"status": TenantStatus.AWAITING_APPROVAL.value})
    suspended = await db.tenants.count_documents({"status": TenantStatus.SUSPENDED.value})
    open_tickets = await db.support_tickets.count_documents(
        {"status": {"$nin": [TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value]}})

    consultants = await db.user_directory.count_documents(
        {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}})
    partners = await db.user_directory.count_documents({"role": Role.PARTNER.value})
    clients = await db.user_directory.count_documents({"role": Role.CLIENT.value})
    accounts_total = consultants + partners + clients

    # Cross-tenant activity (can be slow on huge fleets — fine for platform admin)
    requests_visible = await _sum_across_tenants("requests", {})
    cases_visible = await _sum_across_tenants("cases", {})
    doc_requests_visible = await _sum_across_tenants(
        "documents",
        {"status": DocumentStatus.UPLOAD_NEEDED.value},
    )
    active_cases = await _sum_across_tenants(
        "cases", {"stage": {"$ne": "completed"}})
    docs_pending = await _sum_across_tenants(
        "documents",
        {"status": {"$in": [
            DocumentStatus.UPLOAD_NEEDED.value,
            DocumentStatus.WITH_CONSULTANT.value,
            DocumentStatus.PENDING.value,
            DocumentStatus.NEEDS_REUPLOAD.value,
        ]}},
    )
    revenue_by_plan = []
    for code in PlanCode:
        monthly = round(plan_mrr[code.value], 2)
        pct = round(monthly / mrr * 100, 1) if mrr else 0.0
        revenue_by_plan.append({
            "plan_code": code.value,
            "name": PLANS[code]["name"],
            "mrr": monthly,
            "organizations": plan_org_counts[code.value],
            "pct_of_mrr": pct,
        })

    # Organizations at risk
    at_risk: List[Dict[str, Any]] = []
    async for t in db.tenants.find(
        {"status": {"$in": [
            TenantStatus.SUSPENDED.value,
            TenantStatus.EXPIRED.value,
            TenantStatus.PAST_DUE.value,
            TenantStatus.CANCELLED.value,
        ]}}
    ).sort("updated_at", -1).limit(10):
        tid = str(t["_id"])
        client_count = await tenant_db(tid).users.count_documents(
            {"role": Role.CLIENT.value})
        status = t.get("status")
        label = {
            TenantStatus.SUSPENDED.value: "Suspended",
            TenantStatus.EXPIRED.value: "Subscription expired",
            TenantStatus.PAST_DUE.value: "Failed payment",
            TenantStatus.CANCELLED.value: "Cancelled",
        }.get(status, status)
        at_risk.append({
            "id": tid,
            "name": t.get("name"),
            "status": status,
            "status_label": label,
            "reason": t.get("status_reason") or label,
            "clients": client_count,
            "country": t.get("country"),
            "city": t.get("city"),
            "location": ", ".join(
                p for p in [t.get("city"), t.get("country")] if p) or None,
        })

    # Support queue preview
    support_queue = []
    async for ticket in db.support_tickets.find(
        {"status": {"$nin": [TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value]}}
    ).sort("created_at", -1).limit(5):
        org_name = None
        if ticket.get("tenant_id"):
            org = await db.tenants.find_one({"_id": oid(ticket["tenant_id"])})
            org_name = org["name"] if org else None
        support_queue.append({
            "id": str(ticket["_id"]),
            "reference": ticket.get("reference"),
            "subject": ticket.get("subject"),
            "requester_name": ticket.get("requester_name"),
            "requester_email": ticket.get("requester_email"),
            "organization_name": org_name,
            "status": ticket.get("status"),
            "priority": ticket.get("priority"),
            "created_at": ticket.get("created_at"),
            "created_ago": _relative_time(ticket.get("created_at")),
        })

    # Audit trail preview
    audit_trail = []
    async for row in db.audit_log.find().sort("created_at", -1).limit(10):
        action = row.get("action", "")
        actor = row.get("actor_email") or "System"
        subject = row.get("subject") or ""
        detail = row.get("detail") or ""
        description = detail or f"{actor} · {action}" + (f" · {subject}" if subject else "")
        if action == AuditAction.TENANT_STATUS_CHANGED.value:
            description = (
                f"{row.get('actor_email') or 'Admin'} set organization to "
                f"{subject}" + (f" — {detail}" if detail else "")
            )
        audit_trail.append({
            "id": str(row["_id"]),
            "severity": _audit_severity(action),
            "action": action,
            "description": description,
            "actor_email": row.get("actor_email"),
            "actor_name": row.get("actor_email"),
            "tenant_id": row.get("tenant_id"),
            "created_at": row.get("created_at"),
            "created_ago": _relative_time(row.get("created_at")),
        })

    return {
        "success": True,
        "message": "OK",
        "greeting": f"Platform control — signed in as {first_name}.",
        "signed_in_as": {
            "id": user.id,
            "full_name": user.raw.get("full_name"),
            "email": user.email,
            "admin_role": user.raw.get("admin_role", "super_admin"),
            "title": "Platform Administrator",
        },
        "activity_totals": {
            "requests": requests_visible,
            "cases": cases_visible,
            "document_requests": doc_requests_visible,
            "summary": (
                f"{requests_visible} requests · {cases_visible} cases · "
                f"{doc_requests_visible} document requests visible across every organization."
            ),
        },
        "metrics": {
            "organizations": {
                "active": orgs_active,
                "total": orgs_total,
                "label": "Organizations",
            },
            "monthly_revenue": {
                "mrr": round(mrr, 2),
                "arr": round(mrr * 12, 2),
                "currency": "USD",
                "label": "Monthly revenue",
            },
            "platform_users": {
                "total": accounts_total,
                "consultants": consultants,
                "label": "Platform users",
            },
            "active_cases": {
                "total": active_cases,
                "docs_pending": docs_pending,
                "label": "Active cases",
            },
        },
        "needs_attention": {
            "awaiting_approval": awaiting_approval,
            "failed_payments": past_due_n,
            "open_tickets": open_tickets,
            "suspended": suspended,
        },
        "approval_banner": {
            "show": awaiting_approval > 0,
            "count": awaiting_approval,
            "message": (
                f"{awaiting_approval} organization"
                f"{'s' if awaiting_approval != 1 else ''} signed up and cannot open "
                f"a workspace until you verify and approve "
                f"{'them' if awaiting_approval != 1 else 'it'}."
            ) if awaiting_approval else None,
            "cta": "Review now",
            "href": "/admin/organizations?tab=approval",
        },
        "revenue_by_plan": revenue_by_plan,
        "billing_summary": {
            "collected_to_date": round(collected_to_date, 2),
            "outstanding": round(outstanding, 2),
            "currency": "USD",
        },
        "role_distribution": [
            {
                "role": "consultants",
                "label": "Consultants",
                "count": consultants,
                "description": "Own an organization, run the caseload.",
            },
            {
                "role": "partners",
                "label": "Partners",
                "count": partners,
                "description": "Invited into an organization, deliver tasks.",
            },
            {
                "role": "clients",
                "label": "Clients",
                "count": clients,
                "description": "Submit requests, upload documents.",
            },
        ],
        "organizations_at_risk": at_risk,
        "support_queue": support_queue,
        "audit_trail": audit_trail,
    }


@router.get("/stats", tags=["Super Admin · Dashboard"],
            summary="Compact counters for header badges")
async def stats(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    mrr = 0.0
    async for sub in db.subscriptions.find({"status": "active"}):
        mrr += _monthly_value(sub)
    return {
        "success": True,
        "message": "OK",
        "organizations_total": await db.tenants.count_documents({}),
        "organizations_active": await db.tenants.count_documents(
            {"status": TenantStatus.ACTIVE.value}),
        "awaiting_approval": await db.tenants.count_documents(
            {"status": TenantStatus.AWAITING_APPROVAL.value}),
        "suspended": await db.tenants.count_documents(
            {"status": TenantStatus.SUSPENDED.value}),
        "accounts_total": await db.user_directory.count_documents({}),
        "mrr": round(mrr, 2),
        "arr": round(mrr * 12, 2),
        "open_tickets": await db.support_tickets.count_documents(
            {"status": {"$nin": [TicketStatus.RESOLVED.value,
                                 TicketStatus.CLOSED.value]}}),
        "logins_24h": await db.audit_log.count_documents(
            {"action": AuditAction.LOGIN.value,
             "created_at": {"$gte": utcnow() - timedelta(hours=24)}}),
    }


@router.get("/me", tags=["Super Admin · Dashboard"],
            summary="Signed-in platform administrator profile")
async def admin_me(user: CurrentUser = Depends(require_super_admin)):
    """Alias of ``GET /admin/profile``.

    Kept because clients already call it. It delegates rather than building its
    own reply: the previous version hard-coded `title` and knew nothing about
    `phone` or the avatar, so the dashboard header and the Profile screen
    disagreed about the same account.
    """
    return await get_admin_profile(user)


@router.get("/search", tags=["Super Admin · Dashboard"],
            summary="Global search — organizations, clients, cases, documents")
async def admin_search(q: str = Query(min_length=2),
                       limit: int = Query(8, ge=1, le=25),
                       user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    rx = {"$regex": q, "$options": "i"}

    organizations = [serialize(t) async for t in db.tenants.find(
        {"$or": [{"name": rx}, {"owner_name": rx}, {"owner_email": rx},
                 {"country": rx}, {"city": rx}]}
    ).limit(limit)]

    clients: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    documents: List[Dict[str, Any]] = []

    async for t in db.tenants.find({}, {"_id": 1, "name": 1}).limit(50):
        tid = str(t["_id"])
        tdb = tenant_db(tid)
        if len(clients) < limit:
            async for u in tdb.users.find(
                {"role": Role.CLIENT.value,
                 "$or": [{"full_name": rx}, {"email": rx}]}
            ).limit(limit - len(clients)):
                row = serialize({k: v for k, v in u.items() if k != "password_hash"})
                row["organization_id"] = tid
                row["organization_name"] = t.get("name")
                clients.append(row)
        if len(cases) < limit:
            async for c in tdb.cases.find(
                {"$or": [{"reference": rx}, {"client_name": rx}, {"case_type": rx}]}
            ).limit(limit - len(cases)):
                row = serialize(c)
                row["organization_id"] = tid
                row["organization_name"] = t.get("name")
                cases.append(row)
        if len(documents) < limit:
            async for d in tdb.documents.find({"name": rx}).limit(limit - len(documents)):
                row = serialize(d)
                row["organization_id"] = tid
                row["organization_name"] = t.get("name")
                documents.append(row)

    return {
        "success": True,
        "message": "OK",
        "query": q,
        "organizations": organizations,
        "clients": clients[:limit],
        "cases": cases[:limit],
        "documents": documents[:limit],
    }


router.include_router(organizations_router)
router.include_router(consultants_router)
router.include_router(admin_users_router)
router.include_router(billing_router)
router.include_router(helpdesk_router)
router.include_router(announcements_router)
router.include_router(profile_router)
router.include_router(setup_router)
router.include_router(oversight_router)
router.include_router(settings_router)
router.include_router(notifications_router)
