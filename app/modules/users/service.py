from typing import Any, Dict, Optional

from app.core.deps import CurrentUser
from app.core.enums import Role, UserStatus
from app.core.exceptions import BadRequest, Conflict, Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.partners.service import seat_usage
from app.schemas.common import PageParams
from app.services import invites
from app.services.email import send_client_invite_email, send_partner_invite_email
from app.services.ownership import assign_partner_to_client
from app.services.pagination import paginate


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = serialize(doc)
    out.pop("password_hash", None)
    return out


async def me(db, user: CurrentUser) -> Dict[str, Any]:
    out = _clean(user.raw)
    cid = user.raw.get("consultant_id")
    if cid:
        consultant = await db.users.find_one({"_id": oid(cid)})
        if consultant:
            out["consultant_info"] = {
                "id": str(consultant["_id"]),
                "full_name": consultant.get("full_name"),
                "email": consultant.get("email"),
                "title": consultant.get("title", "Consultant"),
            }
    pdb = platform_db()
    if user.tenant_id:
        tenant = await pdb.tenants.find_one({"_id": oid(user.tenant_id)})
        if tenant:
            out["organization_info"] = {
                "id": str(tenant["_id"]),
                "name": tenant.get("name"),
                "country": tenant.get("country"),
            }
    return out



async def update_profile(db, user: CurrentUser, data) -> Dict[str, Any]:
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    payload["updated_at"] = utcnow()
    await db.users.update_one({"_id": oid(user.id)}, {"$set": payload})
    return _clean(await db.users.find_one({"_id": oid(user.id)}))


async def list_users(db, current_user: CurrentUser, params: PageParams, role: Optional[Role] = None,
                     search: Optional[str] = None,
                     consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if role:
        query["role"] = role.value

    # Multi-SaaS isolation: If logged-in user is a regular consultant, restrict query to their own clients/partners only
    target_cid = consultant_id
    if current_user.role == Role.CONSULTANT:
        target_cid = current_user.id

    if target_cid:
        # Clients have one owner; partners may serve several consultants.
        query["$or"] = [{"consultant_id": target_cid},
                        {"consultant_ids": target_cid}]
    if search:
        search_query = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
        ]
        if "$or" in query:
            query["$and"] = [{"$or": query.pop("$or")}, {"$or": search_query}]
        else:
            query["$or"] = search_query

    page = await paginate(db, "users", query, params, sort=[("created_at", -1)])

    for item in page["items"]:
        item.pop("password_hash", None)
    return page


async def get_user(db, user_id: str) -> Dict[str, Any]:
    doc = await db.users.find_one({"_id": oid(user_id)})
    if not doc:
        raise NotFound("User not found")
    return _clean(doc)


async def invite_team_member(db, user: CurrentUser, tenant: Dict[str, Any],
                             data) -> Dict[str, Any]:
    pdb = platform_db()
    email = data.email.lower()
    if await pdb.user_directory.find_one({"email": email}):
        raise Conflict("Someone with this email already has a WebImove account")

    usage = await seat_usage(db, tenant)
    limit = usage["consultant_seats_limit"]
    if limit is not None and usage["consultant_seats_used"] >= limit:
        raise BadRequest(f"Your plan includes {limit} consultant seat(s). Upgrade to add more.")

    now = utcnow()
    doc = {
        "email": email, "full_name": data.full_name, "mobile": data.mobile,
        "password_hash": None, "role": Role.CONSULTANT.value, "title": data.title,
        "status": UserStatus.INVITED.value, "email_verified": False,
        "invited_by": user.id, "invited_at": now, "created_at": now, "updated_at": now,
    }
    user_id = str((await db.users.insert_one(doc)).inserted_id)
    token = await invites.issue(email=email, tenant_id=user.tenant_id, user_id=user_id,
                                role=Role.CONSULTANT, invited_by=user.id)
    await send_partner_invite_email(to=email, name=data.full_name, org=tenant["name"],
                                    role=data.title, link=invites.build_link(token),
                                    invited_by=user.raw.get("full_name"))
    return _clean({**doc, "_id": oid(user_id)})


async def create_client(db, user: CurrentUser, tenant: Dict[str, Any],
                        data) -> Dict[str, Any]:
    """Consultant-side client creation (the client still sets their own password)."""
    pdb = platform_db()
    email = data.email.lower()
    if await pdb.user_directory.find_one({"email": email}):
        raise Conflict("Someone with this email already has a WebImove account")
    now = utcnow()
    doc = {
        "email": email, "full_name": data.full_name, "mobile": data.mobile,
        "password_hash": None, "role": Role.CLIENT.value,
        "status": UserStatus.INVITED.value, "email_verified": False,
        "nationality": data.nationality, "language": data.language,
        "country_of_residence": data.country_of_residence,
        "consultant_id": user.id, "created_at": now, "updated_at": now,
    }
    user_id = str((await db.users.insert_one(doc)).inserted_id)
    token = await invites.issue(email=email, tenant_id=user.tenant_id, user_id=user_id,
                                role=Role.CLIENT, invited_by=user.id)
    await send_client_invite_email(to=email, name=data.full_name, org=tenant["name"],
                                   link=invites.build_link(token),
                                   consultant=user.raw.get("full_name"))
    return _clean({**doc, "_id": oid(user_id)})


async def get_client_profile_detail(db, user: CurrentUser, client_id: str) -> Dict[str, Any]:
    client = await db.users.find_one({"_id": oid(client_id), "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client profile not found")

    # Access scoping
    if user.role == Role.CONSULTANT and client.get("consultant_id") != user.id:
        raise Forbidden("You do not have access to this client")

    cases_cursor = db.cases.find({"client_id": client_id})
    cases = []
    async for c in cases_cursor:
        cases.append({
            "id": str(c["_id"]),
            "reference": c.get("reference"),
            "case_type": c.get("case_type"),
            "stage": c.get("stage"),
            "status_label": "Under Review" if c.get("stage") == "consultant_review" else "Waiting for documents",
        })

    partner_name = None
    if client.get("partner_id"):
        partner = await db.users.find_one({"_id": oid(client["partner_id"])}, {"full_name": 1})
        partner_name = partner.get("full_name") if partner else None

    return {
        "id": str(client["_id"]),
        "full_name": client.get("full_name", ""),
        "status": client.get("status", "Active"),
        "email": client.get("email"),
        "mobile": client.get("mobile"),
        "country": client.get("country_of_residence") or client.get("nationality"),
        "consultant_id": client.get("consultant_id", user.id),
        "partner_id": client.get("partner_id"),
        "partner_name": partner_name,
        "gdpr_consent_status": "Consent recorded",
        "immigration_cases": cases,
        "banner_notice": "New immigration cases are created after the client submits a request and a consultant approves the recommended process.",
    }


async def assign_partner(db, user: CurrentUser, client_id: str, partner_id: str) -> Dict[str, Any]:
    """
    Bulk-assign one client to one partner in a single action. From here the
    partner processes every one of this client's cases like a consultant would;
    the consultant keeps full visibility to track completion or reassign later.
    """
    client = await db.users.find_one({"_id": oid(client_id), "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client not found")
    if user.role == Role.CONSULTANT and client.get("consultant_id") != user.id:
        raise Forbidden("You do not have access to this client")

    partner = await db.users.find_one({"_id": oid(partner_id), "role": Role.PARTNER.value})
    if not partner:
        raise NotFound("Partner not found")

    await assign_partner_to_client(db, client_id, partner_id)
    return await get_client_profile_detail(db, user, client_id)


async def unassign_partner(db, user: CurrentUser, client_id: str) -> Dict[str, Any]:
    client = await db.users.find_one({"_id": oid(client_id), "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client not found")
    if user.role == Role.CONSULTANT and client.get("consultant_id") != user.id:
        raise Forbidden("You do not have access to this client")

    await assign_partner_to_client(db, client_id, None)
    return await get_client_profile_detail(db, user, client_id)

