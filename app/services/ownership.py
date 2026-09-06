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

from app.core.enums import CONSULTANT_ROLES, Role, TaskStatus
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


async def partner_holds_case(db: AsyncIOMotorDatabase, user, case_id: Optional[str]) -> bool:
    """Whether this partner has an open task on this particular case.

    Delegating a task is a handover of the work on that case: the partner is
    expected to review the documents, approve or return them and re-run the
    analysis, exactly as the consultant would. This is what says they may.

    Scoped to the case the task names rather than to the client, so a partner
    given one piece of work does not thereby gain authority over that client's
    other cases.
    """
    if not case_id or getattr(user, "role", None) != Role.PARTNER:
        return False
    return await db.tasks.find_one(
        {"assignee_id": user.id, "case_id": case_id,
         "status": {"$ne": TaskStatus.CANCELLED.value}},
        {"_id": 1},
    ) is not None


async def assert_client_access(db: AsyncIOMotorDatabase, user, client_id: str,
                               case_id: Optional[str] = None) -> None:
    """
    Gate for anything scoped to one client (their cases, documents, requests).

    Consultants always pass (tenant-wide access). A partner passes when the
    whole client has been handed to them via `assign_partner_to_client`, or —
    when `case_id` is given — when they hold a task on that case.

    Everyone else is refused.
    """
    if getattr(user, "role", None) in CONSULTANT_ROLES:
        return
    if getattr(user, "role", None) == Role.PARTNER:
        client = await db.users.find_one({"_id": oid(client_id)}, {"partner_id": 1})
        if client and client.get("partner_id") == user.id:
            return
        if await partner_holds_case(db, user, case_id):
            return
    raise Forbidden("You do not have access to this client's records")


async def assert_request_access(db: AsyncIOMotorDatabase, user,
                                request_doc: Dict[str, Any]) -> None:
    """Gate for acting on one request: asking for documents, closing it out.

    Consultants always pass. A partner passes on the same rule as everywhere
    else - the whole client handed to them, or a task on the case behind this
    request.

    Which case that is takes two goes. A request carries a `case_id` only once
    its consultation has been completed, and completing it is exactly what the
    partner is here to do - so a request still open has none, and matching on
    it alone would refuse the partner the one action that would create it.
    Delegating a request opens a case up front and stamps that case with the
    `request_id`, so the case is found from the request instead.
    """
    case_id = request_doc.get("case_id")
    if not case_id and getattr(user, "role", None) == Role.PARTNER:
        case = await db.cases.find_one(
            {"request_id": str(request_doc["_id"])}, {"_id": 1})
        if case:
            case_id = str(case["_id"])
    await assert_client_access(db, user, request_doc["client_id"],
                               case_id=case_id)


async def partner_is_delegated(db: AsyncIOMotorDatabase, user, client_id: str) -> bool:
    """Whether this partner has been given work touching this client.

    Two routes count: a whole client handed over with `assign_partner_to_client`,
    or a single task on one of their cases. The second is the everyday one - a
    consultant delegates a translation and the partner needs to know whose
    document it is and what was applied for.

    Read-only. Approving a document or moving a case still needs
    `assert_client_access`, which a task alone does not satisfy.
    """
    if getattr(user, "role", None) != Role.PARTNER:
        return False
    client = await db.users.find_one({"_id": oid(client_id)}, {"partner_id": 1})
    if client and client.get("partner_id") == user.id:
        return True
    return await db.tasks.find_one(
        {"assignee_id": user.id, "client_id": client_id}, {"_id": 1}
    ) is not None


async def assert_case_access(db: AsyncIOMotorDatabase, user, case_doc: Dict[str, Any]) -> None:
    """Same rule as `assert_client_access`, resolved from an already-fetched case.

    The case id is passed through, so a partner holding a task on this case
    passes as well as one the whole client was handed to.
    """
    await assert_client_access(db, user, case_doc["client_id"],
                               case_id=str(case_doc.get("_id") or case_doc.get("id") or ""))


async def assigned_client_ids(db: AsyncIOMotorDatabase, partner_id: str) -> List[str]:
    """Every client a consultant has bulk-assigned to this partner."""
    ids = await db.users.distinct("_id", {"role": Role.CLIENT.value, "partner_id": partner_id})
    return [str(_id) for _id in ids]


async def delegated_case_ids(db: AsyncIOMotorDatabase, partner_id: str) -> List[str]:
    """Every case this partner holds a live task on.

    The everyday delegation: a consultant hands over one case's work, and the
    partner needs its documents listed to be able to do it.
    """
    ids = await db.tasks.distinct(
        "case_id",
        {"assignee_id": partner_id, "case_id": {"$ne": None},
         "status": {"$ne": TaskStatus.CANCELLED.value}},
    )
    return [str(i) for i in ids if i]


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
