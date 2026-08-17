"""
Consultant ownership.

Every tenant-scoped record carries `consultant_id` so "who does this belong to" is
answerable without a join, and indexable. Responses additionally carry a small
`consultant` object so the UI can render the name and avatar without a second call.

Partners are the deliberate exception: a partner belongs to the ORGANIZATION, not
to one consultant ("The partner joins Jenkins Immigration Law only"), and several
consultants may delegate to the same partner. They carry `invited_by` plus a
maintained `consultant_ids` array instead of a single owner.
"""
from typing import Any, Dict, Iterable, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.enums import CONSULTANT_ROLES, Role
from app.core.exceptions import Forbidden
from app.core.utils import oid, utcnow

# Collections that carry a single owning consultant.
OWNED_COLLECTIONS = (
    "requests", "cases", "documents", "tasks", "invoices", "earnings",
    "appointments", "payouts",
)


async def resolve_consultant_id(db: AsyncIOMotorDatabase, *,
                                user=None,
                                request_id: Optional[str] = None,
                                case_id: Optional[str] = None,
                                client_id: Optional[str] = None) -> Optional[str]:
    """
    Work out the owning consultant, most specific source first:
    the acting consultant → the parent case → the parent request → the client's
    assigned consultant → the workspace owner.
    """
    if user is not None and getattr(user, "role", None) in CONSULTANT_ROLES:
        return user.id

    if case_id:
        case = await db.cases.find_one({"_id": oid(case_id)}, {"consultant_id": 1})
        if case and case.get("consultant_id"):
            return case["consultant_id"]

    if request_id:
        req = await db.requests.find_one({"_id": oid(request_id)}, {"consultant_id": 1})
        if req and req.get("consultant_id"):
            return req["consultant_id"]

    if client_id:
        client = await db.users.find_one({"_id": oid(client_id)}, {"consultant_id": 1})
        if client and client.get("consultant_id"):
            return client["consultant_id"]

    owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value}, {"_id": 1})
    return str(owner["_id"]) if owner else None


async def link_partner_to_consultant(db: AsyncIOMotorDatabase, partner_id: str,
                                     consultant_id: Optional[str]) -> None:
    """A partner accumulates consultants as work is delegated — never overwrites."""
    if not partner_id or not consultant_id:
        return
    await db.users.update_one(
        {"_id": oid(partner_id), "role": Role.PARTNER.value},
        {"$addToSet": {"consultant_ids": consultant_id},
         "$set": {"updated_at": utcnow()}},
    )


async def assert_client_access(db: AsyncIOMotorDatabase, user, client_id: str) -> None:
    """
    Gate for anything scoped to one client (their cases, documents, requests).

    Consultants always pass (tenant-wide access). A partner only passes if a
    consultant has bulk-assigned this whole client to them — the "process this
    client's cases like a consultant" delegation — via `assign_partner_to_client`.
    Everyone else is refused.
    """
    if getattr(user, "role", None) in CONSULTANT_ROLES:
        return
    if getattr(user, "role", None) == Role.PARTNER:
        client = await db.users.find_one({"_id": oid(client_id)}, {"partner_id": 1})
        if client and client.get("partner_id") == user.id:
            return
    raise Forbidden("You do not have access to this client's records")


async def assert_case_access(db: AsyncIOMotorDatabase, user, case_doc: Dict[str, Any]) -> None:
    """Same rule as `assert_client_access`, resolved from an already-fetched case."""
    await assert_client_access(db, user, case_doc["client_id"])


async def assigned_client_ids(db: AsyncIOMotorDatabase, partner_id: str) -> List[str]:
    """Every client a consultant has bulk-assigned to this partner."""
    ids = await db.users.distinct("_id", {"role": Role.CLIENT.value, "partner_id": partner_id})
    return [str(_id) for _id in ids]


async def assign_partner_to_client(db: AsyncIOMotorDatabase, client_id: str,
                                   partner_id: Optional[str]) -> None:
    """
    Bulk-assign (or unassign, when partner_id is None) a client to one partner.
    From this point the partner can process every one of that client's cases,
    documents and requests as if they were the consultant — the consultant still
    sees everything and can track completion or reassign at any time.
    """
    await db.users.update_one(
        {"_id": oid(client_id), "role": Role.CLIENT.value},
        {"$set": {"partner_id": partner_id, "updated_at": utcnow()}},
    )
    if partner_id:
        client = await db.users.find_one({"_id": oid(client_id)}, {"consultant_id": 1})
        if client:
            await link_partner_to_consultant(db, partner_id, client.get("consultant_id"))


async def consultant_map(db: AsyncIOMotorDatabase,
                         ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    """One query for every consultant referenced by a page of results."""
    unique = {i for i in ids if i}
    if not unique:
        return {}
    valid = []
    for i in unique:
        try:
            valid.append(oid(i))
        except Exception:  # noqa: BLE001 - ignore malformed legacy ids
            continue
    if not valid:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    cursor = db.users.find(
        {"_id": {"$in": valid}},
        {"full_name": 1, "title": 1, "avatar_url": 1, "email": 1, "role": 1},
    )
    async for doc in cursor:
        out[str(doc["_id"])] = {
            "id": str(doc["_id"]),
            "full_name": doc.get("full_name"),
            "title": doc.get("title") or "Consultant",
            "avatar_url": doc.get("avatar_url"),
            "email": doc.get("email"),
            "is_owner": doc.get("role") == Role.CONSULTANT_OWNER.value,
        }
    return out


def _ids_in(items: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    for item in items:
        if item.get("consultant_id"):
            ids.append(item["consultant_id"])
        for extra in item.get("consultant_ids") or []:
            ids.append(extra)
    return ids


async def attach_consultants(db: AsyncIOMotorDatabase,
                             items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Adds `consultant` (and `consultants` for partners) to already-serialized docs."""
    if not items:
        return items
    lookup = await consultant_map(db, _ids_in(items))
    if not lookup:
        return items
    for item in items:
        if item.get("consultant_id"):
            item["consultant"] = lookup.get(item["consultant_id"])
        if item.get("consultant_ids"):
            item["consultants"] = [lookup[c] for c in item["consultant_ids"]
                                   if c in lookup]
    return items


async def attach_consultant(db: AsyncIOMotorDatabase,
                            item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item:
        return item
    (await attach_consultants(db, [item]))
    return item
