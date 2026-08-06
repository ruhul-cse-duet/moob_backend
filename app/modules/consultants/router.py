"""
Who works under whom.

One consultant owns a client outright (`consultant_id`). A partner belongs to the
organization and may be delegated to by several consultants, so partners carry a
`consultant_ids` array rather than a single owner.
"""


from fastapi import APIRouter, Body, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
)
from app.core.enums import CONSULTANT_ROLES, Role, TaskStatus
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services.events import log_activity

from app.services.pagination import paginate

router = APIRouter(prefix="/consultants", tags=["Consultants & Ownership"],
                   dependencies=[Depends(require_active_tenant)])

_ROLES = [r.value for r in CONSULTANT_ROLES]


@router.get("", summary="Consultants in this workspace, with their caseload")
async def list_consultants(user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    rows = []
    async for c in db.users.find({"role": {"$in": _ROLES}}).sort("created_at", 1):
        cid = str(c["_id"])
        rows.append({
            "id": cid,
            "full_name": c.get("full_name"),
            "title": c.get("title") or "Consultant",
            "email": c.get("email"),
            "avatar_url": c.get("avatar_url"),
            "is_owner": c.get("role") == Role.CONSULTANT_OWNER.value,
            "status": c.get("status"),
            "clients": await db.users.count_documents(
                {"role": Role.CLIENT.value, "consultant_id": cid}),
            "partners": await db.users.count_documents(
                {"role": Role.PARTNER.value, "consultant_ids": cid}),
            "open_requests": await db.requests.count_documents(
                {"consultant_id": cid, "status": {"$ne": "completed"}}),
            "active_cases": await db.cases.count_documents(
                {"consultant_id": cid, "stage": {"$ne": "completed"}}),
            "open_tasks": await db.tasks.count_documents(
                {"consultant_id": cid,
                 "status": {"$in": [TaskStatus.PENDING.value,
                                    TaskStatus.IN_PROGRESS.value]}}),
        })
    return {"items": rows, "total": len(rows)}


@router.get("/me/roster", summary="My own clients, partners and workload")
async def my_roster(user: CurrentUser = Depends(require_consultant),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Declared before /{consultant_id}/roster so "me" is not parsed as an id."""
    return await roster(user.id, user, db)


@router.get("/{consultant_id}/roster",
            summary="Everyone and everything under one consultant")
async def roster(consultant_id: str,
                 user: CurrentUser = Depends(get_current_user),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    consultant = await db.users.find_one({"_id": oid(consultant_id),
                                          "role": {"$in": _ROLES}})
    if not consultant:
        raise NotFound("Consultant not found")

    def clean(doc):
        out = serialize(doc)
        out.pop("password_hash", None)
        return out

    return {
        "consultant": clean(consultant),
        "clients": [clean(c) async for c in db.users.find(
            {"role": Role.CLIENT.value, "consultant_id": consultant_id})],
        "partners": [clean(p) async for p in db.users.find(
            {"role": Role.PARTNER.value, "consultant_ids": consultant_id})],
        "requests": [serialize(r) async for r in db.requests.find(
            {"consultant_id": consultant_id}).sort("created_at", -1).limit(50)],
        "cases": [serialize(c) async for c in db.cases.find(
            {"consultant_id": consultant_id}).sort("created_at", -1).limit(50)],
    }


@router.get("/{consultant_id}/clients", summary="Clients under one consultant")
async def consultant_clients(consultant_id: str,
                             params: PageParams = Depends(page_params),
                             user: CurrentUser = Depends(get_current_user),
                             db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    page = await paginate(db, "users",
                          {"role": Role.CLIENT.value, "consultant_id": consultant_id},
                          params, sort=[("created_at", -1)])
    for item in page["items"]:
        item.pop("password_hash", None)
    return page


@router.get("/{consultant_id}/partners",
            summary="Partners this consultant has delegated to")
async def consultant_partners(consultant_id: str,
                              params: PageParams = Depends(page_params),
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    page = await paginate(db, "users",
                          {"role": Role.PARTNER.value,
                           "consultant_ids": consultant_id},
                          params, sort=[("created_at", -1)])
    for item in page["items"]:
        item.pop("password_hash", None)
    return page


@router.post("/clients/{client_id}/assign",
             summary="Move a client to a different consultant")
async def assign_client(client_id: str,
                        consultant_id: str = Body(embed=True),
                        reassign_open_work: bool = Body(True, embed=True),
                        user: CurrentUser = Depends(require_consultant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    consultant = await db.users.find_one({"_id": oid(consultant_id),
                                          "role": {"$in": _ROLES}})
    if not consultant:
        raise BadRequest("That user is not a consultant in this workspace")
    client = await db.users.find_one({"_id": oid(client_id), "role": Role.CLIENT.value})
    if not client:
        raise NotFound("Client not found")

    now = utcnow()
    await db.users.update_one({"_id": oid(client_id)},
                              {"$set": {"consultant_id": consultant_id,
                                        "updated_at": now}})
    moved = {}
    if reassign_open_work:
        # Completed work keeps its original owner - reassigning it would rewrite history.
        for coll, filt in (
            ("requests", {"client_id": client_id, "status": {"$ne": "completed"}}),
            ("cases", {"client_id": client_id, "stage": {"$ne": "completed"}}),
            ("documents", {"client_id": client_id}),
            ("tasks", {"client_id": client_id,
                       "status": {"$in": [TaskStatus.PENDING.value,
                                          TaskStatus.IN_PROGRESS.value]}}),
        ):
            result = await db[coll].update_many(
                filt, {"$set": {"consultant_id": consultant_id, "updated_at": now}})
            moved[coll] = result.modified_count

    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action=f"reassigned to {consultant.get('full_name')}",
                       subject=client.get("full_name", "client"))
    return {"detail": f"{client.get('full_name')} now reports to "
                      f"{consultant.get('full_name')}",
            "client_id": client_id, "consultant_id": consultant_id,
            "reassigned": moved}

