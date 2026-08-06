from fastapi import APIRouter

from app.modules.admin.router import router as admin_router
from app.modules.agenda.router import router as agenda_router
from app.modules.ai.router import router as ai_router
from app.modules.auth.router import router as auth_router
from app.modules.billing.router import router as invoices_router
from app.modules.cases.reports import router as analysis_router
from app.modules.cases.router import router as cases_router
from app.modules.consultants.router import router as consultants_router
from app.modules.earnings.router import router as earnings_router
from app.modules.documents.router import router as documents_router
from app.modules.legal.router import router as legal_router
from app.modules.messages.router import router as messages_router
from app.modules.notifications.router import router as notifications_router
from app.modules.partners.router import router as partners_router
from app.modules.reporting.router import router as reporting_router
from app.modules.requests.router import router as requests_router
from app.modules.privacy.router import router as privacy_router
from app.modules.search.router import router as search_router
from app.modules.security.router import router as security_router
from app.modules.support.router import router as support_router
from app.modules.subscriptions.router import router as subscription_router
from app.modules.tasks.router import router as tasks_router
from app.modules.tenants.router import router as organization_router
from app.modules.users.router import router as users_router

api_router = APIRouter()

# Shared by the mobile apps AND the consultant website - one API, role-scoped.
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(organization_router)
api_router.include_router(subscription_router)
api_router.include_router(consultants_router)
api_router.include_router(partners_router)
api_router.include_router(requests_router)
api_router.include_router(documents_router)
api_router.include_router(cases_router)
api_router.include_router(analysis_router)
api_router.include_router(tasks_router)
api_router.include_router(ai_router)
api_router.include_router(agenda_router)
api_router.include_router(messages_router)
api_router.include_router(notifications_router)
api_router.include_router(earnings_router)
api_router.include_router(invoices_router)
api_router.include_router(support_router)
api_router.include_router(privacy_router)
api_router.include_router(security_router)
api_router.include_router(legal_router)
api_router.include_router(reporting_router)
api_router.include_router(search_router)
api_router.include_router(admin_router)
