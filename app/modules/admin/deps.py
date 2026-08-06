from fastapi import Depends

from app.core.deps import CurrentUser, require_super_admin
from app.core.enums import AdminRole
from app.core.exceptions import Forbidden


def require_admin_roles(*roles: AdminRole):
    """Platform staff are all authenticated as super_admin; admin_role narrows it. [INFERRED]"""
    allowed = {r.value for r in roles}

    async def _guard(user: CurrentUser = Depends(require_super_admin)) -> CurrentUser:
        admin_role = user.raw.get("admin_role", AdminRole.SUPER_ADMIN.value)
        if admin_role != AdminRole.SUPER_ADMIN.value and admin_role not in allowed:
            raise Forbidden("Your admin role cannot perform this action")
        return user

    return _guard


require_billing_admin = require_admin_roles(AdminRole.BILLING_ADMIN)
require_support_agent = require_admin_roles(AdminRole.SUPPORT_AGENT)
