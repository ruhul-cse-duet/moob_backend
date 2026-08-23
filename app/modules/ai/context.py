"""What the assistant is allowed to know about the caller's workspace.

The assistant used to be handed nothing but a name and a role, so a consultant
asking "what is going on with the ccc client?" got a paragraph of invention
about an organisation that does not exist. It now answers from the same records
the caller could open in the app - and from nothing else.

Everything here is scoped the way the rest of the API is scoped: a consultant
sees their organization, a partner sees the work delegated to them, a client
sees themselves. The snapshot is deliberately small: a prompt is not a database,
and a hundred cases of detail would crowd out the question.
"""
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.enums import CONSULTANT_ROLES, Role
from app.core.utils import oid
from app.services.ownership import assigned_client_ids

# Per list. Enough that a working consultancy sees its real caseload, small
# enough that the model still has room to think about the question.
LIMIT = 25


def _date(value) -> Optional[str]:
    return value.date().isoformat() if value else None


def _cap(rows: List[Dict[str, Any]], total: int) -> Dict[str, Any]:
    """A list plus the truth about what was left out.

    Without the count the model reads a truncated list as the whole picture and
    answers "you have 25 cases" to a consultancy with two hundred.
    """
    out: Dict[str, Any] = {"total": total, "items": rows}
    if total > len(rows):
        out["note"] = f"showing {len(rows)} of {total}; ask about one by name for detail"
    return out


async def _count(db, collection: str, query: Dict[str, Any]) -> int:
    return await db[collection].count_documents(query)


async def workspace_snapshot(db: AsyncIOMotorDatabase, user) -> Dict[str, Any]:
    """The records this account may see, shaped for a prompt."""
    if user.role in CONSULTANT_ROLES:
        return await _consultant_snapshot(db, user)
    if user.role == Role.PARTNER:
        return await _partner_snapshot(db, user)
    if user.role == Role.CLIENT:
        return await _client_snapshot(db, user)
    return {}


async def _case_rows(db, query: Dict[str, Any]) -> List[Dict[str, Any]]:
    cases = [c async for c in db.cases.find(query).sort("updated_at", -1).limit(LIMIT)]
    if not cases:
        return []

    # One grouped read rather than two counts per case: this runs on every chat
    # message, and a full page of cases would otherwise be fifty round trips to
    # the database before the assistant has even seen the question.
    ids = [str(case["_id"]) for case in cases]
    tally: Dict[str, Dict[str, int]] = {}
    async for row in db.documents.aggregate([
        {"$match": {"case_id": {"$in": ids}}},
        {"$group": {"_id": {"case": "$case_id", "status": "$status"},
                    "n": {"$sum": 1}}},
    ]):
        tally.setdefault(row["_id"]["case"], {})[row["_id"]["status"]] = row["n"]

    rows = []
    for case in cases:
        counts = tally.get(str(case["_id"]), {})
        rows.append({
            "reference": case.get("reference"),
            "client": case.get("client_name"),
            "type": case.get("case_type"),
            "stage": case.get("stage"),
            "progress_percent": case.get("progress"),
            "destination": case.get("destination_country"),
            "deadline": _date(case.get("deadline")),
            "documents_awaiting_review": counts.get("submitted", 0),
            "documents_approved": counts.get("approved", 0),
            "documents_rejected": counts.get("rejected", 0),
        })
    return rows


async def _request_rows(db, query: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    async for req in db.requests.find(query).sort("created_at", -1).limit(LIMIT):
        rows.append({
            "client": req.get("client_name"),
            "reference": req.get("reference"),
            "visa_type": req.get("visa_type"),
            "purpose": req.get("purpose"),
            "destination": req.get("destination_country"),
            "status": req.get("status"),
            "submitted": _date(req.get("created_at")),
        })
    return rows


async def _task_rows(db, query: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    async for task in db.tasks.find(query).sort("created_at", -1).limit(LIMIT):
        rows.append({
            "title": task.get("title"),
            "case": task.get("case_reference"),
            "client": task.get("client_name"),
            "assigned_to": task.get("assignee_name"),
            "status": task.get("status"),
            "due": _date(task.get("due_date")),
        })
    return rows


async def _consultant_snapshot(db, user) -> Dict[str, Any]:
    clients = []
    async for row in db.users.find(
        {"role": Role.CLIENT.value}, {"full_name": 1, "email": 1, "status": 1,
                                      "nationality": 1, "country_of_residence": 1},
    ).sort("created_at", -1).limit(LIMIT):
        clients.append({
            "name": row.get("full_name"),
            "email": row.get("email"),
            "status": row.get("status"),
            "nationality": row.get("nationality"),
            "lives_in": row.get("country_of_residence"),
        })

    partners = []
    async for row in db.users.find(
        {"role": Role.PARTNER.value}, {"full_name": 1, "partner_role": 1, "status": 1},
    ).limit(LIMIT):
        partners.append({
            "name": row.get("full_name"),
            "does": row.get("partner_role"),
            "status": row.get("status"),
        })

    return {
        "scope": "every client, case and request in this organization",
        "clients": _cap(clients, await _count(db, "users", {"role": Role.CLIENT.value})),
        "cases": _cap(await _case_rows(db, {}), await _count(db, "cases", {})),
        "requests": _cap(await _request_rows(db, {}), await _count(db, "requests", {})),
        "partners": _cap(partners, await _count(db, "users", {"role": Role.PARTNER.value})),
        "delegated_tasks": _cap(
            await _task_rows(db, {"assignee_type": "partner"}),
            await _count(db, "tasks", {"assignee_type": "partner"})),
    }


async def _partner_snapshot(db, user) -> Dict[str, Any]:
    # Two ways in: a task on a single case, or a whole client handed over.
    case_ids = await db.tasks.distinct("case_id", {"assignee_id": user.id})
    client_ids = await assigned_client_ids(db, user.id)
    case_query: Dict[str, Any] = {"$or": [
        {"_id": {"$in": [oid(cid) for cid in case_ids if cid]}},
        {"client_id": {"$in": client_ids}},
    ]}
    return {
        "scope": "only the cases delegated to you and the clients assigned to you",
        "cases": _cap(await _case_rows(db, case_query),
                      await _count(db, "cases", case_query)),
        "my_tasks": _cap(await _task_rows(db, {"assignee_id": user.id}),
                         await _count(db, "tasks", {"assignee_id": user.id})),
    }


async def _client_snapshot(db, user) -> Dict[str, Any]:
    documents = []
    async for doc in db.documents.find(
        {"client_id": user.id},
        {"name": 1, "status": 1, "category": 1, "consultant_feedback": 1},
    ).sort("created_at", -1).limit(LIMIT):
        documents.append({
            "name": doc.get("name"),
            "category": doc.get("category"),
            "status": doc.get("status"),
            "consultant_note": doc.get("consultant_feedback"),
        })

    return {
        "scope": "your own cases, requests and documents",
        "cases": _cap(await _case_rows(db, {"client_id": user.id}),
                      await _count(db, "cases", {"client_id": user.id})),
        "requests": _cap(await _request_rows(db, {"client_id": user.id}),
                         await _count(db, "requests", {"client_id": user.id})),
        "documents": _cap(documents,
                          await _count(db, "documents", {"client_id": user.id})),
    }
