"""
Super Admin surface. Assembled from the Figma Make page inventory:
AdminDashboard, admin/Organizations, admin/OrganizationDetail, admin/AdminUsers,
admin/Billing, admin/Helpdesk, admin/Oversight, admin/PlatformSettings.

[INFERRED] The Make source was not readable through the Figma MCP, so response
shapes come from the page names plus the domain. Verify against
src/store/PlatformStore.tsx and src/store/AdminStore.tsx.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import AuditAction, PlanCode, Role, TenantStatus, TicketStatus
from app.core.utils import serialize, utcnow
from app.db.mongo import platform_db
from app.modules.admin.admin_users import router as admin_users_router
from app.modules.admin.billing import _monthly_value
from app.modules.admin.billing import router as billing_router
from app.modules.admin.helpdesk import router as helpdesk_router
from app.modules.admin.organizations import router as organizations_router
from app.modules.admin.oversight import router as oversight_router
from app.modules.admin.settings import router as settings_router

router = APIRouter(prefix="/admin")


@router.get("/dashboard", tags=["Super Admin · Dashboard"],
            summary="AdminDashboard.tsx — platform overview")
async def dashboard(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    now = utcnow()
    last_30 = now - timedelta(days=30)
    prev_30 = now - timedelta(days=60)

    mrr = 0.0
    async for sub in db.subscriptions.find({"status": "active"}):
        mrr += _monthly_value(sub)

    new_30 = await db.tenants.count_documents({"created_at": {"$gte": last_30}})
    prev_new_30 = await db.tenants.count_documents(
        {"created_at": {"$gte": prev_30, "$lt": last_30}})

    return {
        "counters": {
            "organizations_total": await db.tenants.count_documents({}),
            "organizations_active": await db.tenants.count_documents(
                {"status": TenantStatus.ACTIVE.value}),
            "organizations_pending_payment": await db.tenants.count_documents(
                {"status": TenantStatus.PENDING_PAYMENT.value}),
            "accounts_total": await db.user_directory.count_documents({}),
            "consultants": await db.user_directory.count_documents(
                {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}}),
            "partners": await db.user_directory.count_documents({"role": Role.PARTNER.value}),
            "clients": await db.user_directory.count_documents({"role": Role.CLIENT.value}),
            "mrr": round(mrr, 2),
            "arr": round(mrr * 12, 2),
            "open_tickets": await db.support_tickets.count_documents(
                {"status": {"$nin": [TicketStatus.RESOLVED.value,
                                     TicketStatus.CLOSED.value]}}),
        },
        "growth": {
            "new_organizations_30d": new_30,
            "previous_30d": prev_new_30,
            "change_pct": round((new_30 - prev_new_30) / prev_new_30 * 100, 1)
            if prev_new_30 else None,
        },
        "plan_mix": {code.value: await db.tenants.count_documents({"plan_code": code.value})
                     for code in PlanCode},
        "recent_organizations": [serialize(t) async for t in
                                 db.tenants.find().sort("created_at", -1).limit(5)],
        "recent_audit": [serialize(a) async for a in
                         db.audit_log.find().sort("created_at", -1).limit(10)],
        "recent_tickets": [serialize(t) async for t in
                           db.support_tickets.find(
                               {"status": {"$nin": [TicketStatus.RESOLVED.value,
                                                    TicketStatus.CLOSED.value]}})
                           .sort("created_at", -1).limit(5)],
    }


@router.get("/stats", tags=["Super Admin · Dashboard"], summary="Compact platform counters")
async def stats(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    mrr = 0.0
    async for sub in db.subscriptions.find({"status": "active"}):
        mrr += _monthly_value(sub)
    return {
        "tenants_total": await db.tenants.count_documents({}),
        "tenants_active": await db.tenants.count_documents({"status": TenantStatus.ACTIVE.value}),
        "tenants_cancelled": await db.tenants.count_documents(
            {"status": TenantStatus.CANCELLED.value}),
        "accounts_total": await db.user_directory.count_documents({}),
        "mrr_estimate": round(mrr, 2),
        "logins_24h": await db.audit_log.count_documents(
            {"action": AuditAction.LOGIN.value,
             "created_at": {"$gte": utcnow() - timedelta(hours=24)}}),
    }


router.include_router(organizations_router)
router.include_router(admin_users_router)
router.include_router(billing_router)
router.include_router(helpdesk_router)
router.include_router(oversight_router)
router.include_router(settings_router)
