from typing import Any, Dict, Optional

from app.core.deps import CurrentUser
from app.core.enums import Role, UserStatus
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.modules.partners.service import seat_usage
from app.schemas.common import PageParams
from app.services import invites
from app.services.email import send_client_invite_email, send_partner_invite_email
from app.services.pagination import paginate


def _clean(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = serialize(doc)
    out.pop("password_hash", None)
    return out


async def me(db, user: CurrentUser) -> Dict[str, Any]:
    return _clean(user.raw)


async def update_profile(db, user: CurrentUser, data) -> Dict[str, Any]:
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    payload["updated_at"] = utcnow()
    await db.users.update_one({"_id": oid(user.id)}, {"$set": payload})
    return _clean(await db.users.find_one({"_id": oid(user.id)}))


async def list_users(db, params: PageParams, role: Optional[Role] = None,
                     search: Optional[str] = None,
                     consultant_id: Optional[str] = None) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if role:
        query["role"] = role.value
    if consultant_id:
        # Clients have one owner; partners may serve several consultants.
        query["$or"] = [{"consultant_id": consultant_id},
                        {"consultant_ids": consultant_id}]
    if search:
        query["$or"] = [
            {"full_name": {"$regex": search, "$options": "i"}},
            {"email": {"$regex": search, "$options": "i"}},
        ]
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
