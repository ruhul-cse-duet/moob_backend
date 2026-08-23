from typing import Any, Dict, Optional

from app.core.deps import CurrentUser
from app.core.enums import Role, TaskStatus, UserStatus
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.core.config import settings
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.subscriptions.plans import PLANS
from app.core.enums import PlanCode
from app.schemas.common import PageParams
from app.services import invites
from app.services.email import send_partner_invite_email
from app.services.pagination import paginate


async def _seat_limits(tenant: Dict[str, Any]) -> Dict[str, Optional[int]]:
    plan = PLANS[PlanCode(tenant["plan_code"])]
    return {"consultant": plan["consultant_seats"], "partner": plan["partner_seats"]}


async def seat_usage(db, tenant: Dict[str, Any]) -> Dict[str, Any]:
    limits = await _seat_limits(tenant)
    consultants = await db.users.count_documents(
        {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}}
    )
    partners = await db.users.count_documents({"role": Role.PARTNER.value})
    return {
        "consultant_seats_used": consultants,
        "consultant_seats_limit": limits["consultant"],
        "partner_seats_used": partners,
        "partner_seats_limit": limits["partner"],
    }


async def invite_partner(db, user: CurrentUser, tenant: Dict[str, Any],
                         data) -> Dict[str, Any]:
    pdb = platform_db()
    email = data.email.lower()
    if await pdb.user_directory.find_one({"email": email}):
        raise Conflict("Someone with this email already has a WebImove account")

    usage = await seat_usage(db, tenant)
    limit = usage["partner_seats_limit"]
    if limit is not None and usage["partner_seats_used"] >= limit:
        raise BadRequest(
            f"Your plan allows {limit} partners. Upgrade the subscription to invite more."
        )

    now = utcnow()
    doc = {
        "email": email,
        "full_name": data.full_name,
        "mobile": data.mobile,
        "password_hash": None,
        "role": Role.PARTNER.value,
        "partner_role": data.role,
        "status": UserStatus.INVITED.value,
        "email_verified": False,
        # The inviting consultant OWNS this partner - they sign in and land under them.
        # `consultant_ids` additionally tracks every consultant who delegates work,
        # so a second consultant assigning a task never steals ownership.
        "consultant_id": user.id,
        "invited_by": user.id,
        "consultant_ids": [user.id],
        "invited_at": now,
        "created_at": now,
        "updated_at": now,
    }
    user_id = str((await db.users.insert_one(doc)).inserted_id)
    token = await invites.issue(email=email, tenant_id=user.tenant_id, user_id=user_id,
                                role=Role.PARTNER, invited_by=user.id)
    emailed = await send_partner_invite_email(
        to=email, name=data.full_name, org=tenant["name"], role=data.role,
        link=invites.build_link(token),
        invited_by=user.raw.get("full_name"),
    )
    out = serialize({**doc, "_id": oid(user_id)})
    out["invite_expires_in_days"] = settings.INVITE_EXPIRE_DAYS
    # Mail is the one step here that can fail without failing the request. Say
    # so, or the consultant walks away believing an invitation is on its way.
    out["invite_email_sent"] = emailed
    # The consultant may want to pass the code on by hand — the email is not
    # the only route in.
    out["invite_token"] = token
    out["invite_link"] = invites.build_link(token)
    return out


async def list_partners(db, params: PageParams, search: Optional[str] = None,
                        consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {"role": Role.PARTNER.value}
    if consultant_id:
        query["consultant_ids"] = consultant_id
    if search:
        query["$or"] = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
            {"partner_role": {"$regex": search, "$options": "i"}},
        ]
    page = await paginate(db, "users", query, params, sort=[("created_at", -1)])
    pdb = platform_db()
    for item in page["items"]:
        item.pop("password_hash", None)
        item["open_tasks"] = await db.tasks.count_documents({
            "assignee_id": item["id"],
            "status": {"$in": [TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value]},
        })
        # A partner who has not signed in yet still has a live invite code, and
        # the workspace screen offers it for copying.
        if item.get("status") == UserStatus.INVITED.value:
            entry = await pdb.user_directory.find_one({"email": item.get("email")})
            token = (entry or {}).get("invite_token")
            if token:
                item["invite_token"] = token
                item["invite_link"] = invites.build_link(token)
    return page


async def resend_invite(db, tenant: Dict[str, Any], partner_id: str) -> Dict[str, str]:
    partner = await db.users.find_one({"_id": oid(partner_id), "role": Role.PARTNER.value})
    if not partner:
        raise NotFound("Partner not found")
    if partner["status"] != UserStatus.INVITED.value:
        raise BadRequest("This partner has already joined")
    # New token, new expiry - the previous link stops working immediately.
    token = await invites.refresh(partner["email"])
    await db.users.update_one({"_id": oid(partner_id)}, {"$set": {"invited_at": utcnow()}})
    emailed = await send_partner_invite_email(
        to=partner["email"], name=partner["full_name"], org=tenant["name"],
        role=partner.get("partner_role", "Partner"),
        link=invites.build_link(token),
    )
    return {
        "detail": (
            f"Invitation resent. The link is valid for "
            f"{settings.INVITE_EXPIRE_DAYS} days."
        ) if emailed else (
            "We could not email the invitation. Send them the code below "
            f"instead - it is valid for {settings.INVITE_EXPIRE_DAYS} days."
        ),
        "invite_token": token,
        "invite_link": invites.build_link(token),
        "invite_email_sent": emailed,
    }


async def revoke_partner(db, partner_id: str) -> Dict[str, str]:
    partner = await db.users.find_one({"_id": oid(partner_id), "role": Role.PARTNER.value})
    if not partner:
        raise NotFound("Partner not found")
    open_tasks = await db.tasks.count_documents({
        "assignee_id": partner_id,
        "status": {"$in": [TaskStatus.PENDING.value, TaskStatus.IN_PROGRESS.value]},
    })
    if open_tasks:
        raise BadRequest(f"Reassign {open_tasks} open task(s) before revoking this partner")
    await db.users.delete_one({"_id": oid(partner_id)})
    await invites.revoke(partner["email"])
    return {"detail": "Partner access revoked"}


async def suspend_partner(db, partner_id: str, suspend: bool = True) -> Dict[str, Any]:
    status = UserStatus.SUSPENDED.value if suspend else UserStatus.ACTIVE.value
    result = await db.users.find_one_and_update(
        {"_id": oid(partner_id), "role": Role.PARTNER.value},
        {"$set": {"status": status, "updated_at": utcnow()}},
        return_document=True,
    )
    if not result:
        raise NotFound("Partner not found")
    result.pop("password_hash", None)
    return serialize(result)
