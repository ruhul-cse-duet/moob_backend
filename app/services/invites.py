"""
Invitation tokens for partners, team consultants and consultant-created clients.

Flow (identical for all three roles):
  1. A consultant creates the account. It exists with status `invited` and no password.
  2. An email goes out with a tokenised link to the FRONTEND, not this API.
  3. The frontend calls GET /auth/invite/{token} to show who invited them and for what.
  4. They set a password via POST /auth/invite/accept. The account activates and they
     are signed in immediately.

The token lives on the platform `user_directory` entry, because that is the record the
login path already uses to map an email to a tenant database. One place, not two.
"""
from datetime import timedelta
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.enums import Role, UserStatus
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import oid, random_token, utcnow
from app.db.mongo import platform_db, tenant_db


def build_link(token: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}{settings.INVITE_ACCEPT_PATH}?token={token}"


async def issue(
    *,
    email: str,
    tenant_id: str,
    user_id: str,
    role: Role,
    invited_by: Optional[str] = None,
) -> str:
    """Create (or replace) the directory entry that carries the invite token."""
    token = random_token(40)
    now = utcnow()
    await platform_db().user_directory.update_one(
        {"email": email.lower()},
        {"$set": {
            "email": email.lower(),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "role": role.value,
            "invite_token": token,
            "invited_by": invited_by,
            "invited_at": now,
            "invite_expires_at": now + timedelta(days=settings.INVITE_EXPIRE_DAYS),
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return token


async def refresh(email: str) -> str:
    """Resend: new token, new expiry. The old link stops working immediately."""
    entry = await platform_db().user_directory.find_one({"email": email.lower()})
    if not entry:
        raise NotFound("No invitation for that email")
    return await issue(email=email, tenant_id=entry["tenant_id"],
                       user_id=entry["user_id"], role=Role(entry["role"]),
                       invited_by=entry.get("invited_by"))


def _now_like(value):
    now = utcnow()
    return now if value.tzinfo else now.replace(tzinfo=None)


async def lookup(token: str) -> Dict[str, Any]:
    """Validate a token and describe the invitation. Never leaks a password hash."""
    db = platform_db()
    entry = await db.user_directory.find_one({"invite_token": token})
    if not entry:
        raise NotFound("This invitation link is invalid or has already been used")

    expires = entry.get("invite_expires_at")
    if expires and expires < _now_like(expires):
        raise BadRequest(
            "This invitation has expired. Ask your consultant to send a new one."
        )

    tenant = await db.tenants.find_one({"_id": oid(entry["tenant_id"])})
    tdb = tenant_db(entry["tenant_id"])
    user = await tdb.users.find_one({"_id": oid(entry["user_id"])})
    if not user:
        raise NotFound("The invited account no longer exists")
    if user.get("status") == UserStatus.ACTIVE.value and user.get("password_hash"):
        raise BadRequest("This invitation was already accepted. Sign in instead.")

    inviter = None
    if entry.get("invited_by"):
        doc = await tdb.users.find_one({"_id": oid(entry["invited_by"])})
        if doc:
            inviter = {"id": str(doc["_id"]), "full_name": doc.get("full_name"),
                       "title": doc.get("title") or "Consultant",
                       "avatar_url": doc.get("avatar_url")}

    return {
        "entry": entry,
        "user": user,
        "tenant": tenant,
        "preview": {
            "email": entry["email"],
            "full_name": user.get("full_name"),
            "role": entry["role"],
            "partner_role": user.get("partner_role"),
            "title": user.get("title"),
            "organization": {"id": str(tenant["_id"]), "name": tenant["name"]}
            if tenant else None,
            "invited_by": inviter,
            "invited_at": entry.get("invited_at"),
            "expires_at": expires,
        },
    }


async def states_for(emails) -> Dict[str, str]:
    """Where each of these invitations stands: `invited`, `expired` or `accepted`.

    One query for the whole list rather than one per row - a client list is
    twenty of these, and the answer lives in a different database from the users
    being listed.

    An entry with no token left has been consumed, which is the only way a token
    disappears; one whose expiry has passed is the state a consultant most needs
    to see, because it looks identical to "invited" on the user row and means
    the opposite.
    """
    wanted = [str(e).lower() for e in emails if e]
    if not wanted:
        return {}

    out: Dict[str, str] = {}
    async for entry in platform_db().user_directory.find(
        {"email": {"$in": wanted}},
        {"email": 1, "invite_token": 1, "invite_expires_at": 1},
    ):
        if not entry.get("invite_token"):
            out[entry["email"]] = "accepted"
            continue
        expires = entry.get("invite_expires_at")
        out[entry["email"]] = (
            "expired" if expires and expires < _now_like(expires) else "invited"
        )
    return out


async def consume(token: str) -> Dict[str, Any]:
    """Same validation as lookup, then burn the token so the link is single-use."""
    found = await lookup(token)
    await platform_db().user_directory.update_one(
        {"_id": found["entry"]["_id"]},
        {"$unset": {"invite_token": "", "invite_expires_at": ""},
         "$set": {"accepted_at": utcnow()}},
    )
    return found


async def revoke(email: str) -> None:
    await platform_db().user_directory.delete_one({"email": email.lower()})
