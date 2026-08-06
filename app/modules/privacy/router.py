"""
client/ClientConsent.tsx + client/PrivacyCentre.tsx  [INFERRED]

GDPR surface: granular consent toggles, data export, deletion request.
The design file repeatedly says "GDPR-ready by design", so these are modelled as
auditable records with timestamps and IP, not as booleans on the user document.
"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request, status as http
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, get_current_user, get_tenant_db, page_params
from app.core.enums import (
    AuditAction,
    ConsentType,
    DataRequestStatus,
    DataRequestType,
    NotificationType,
    Role,
)
from app.core.exceptions import BadRequest, NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import PageParams
from app.services import audit
from app.services.events import notify
from app.services.pagination import paginate

router = APIRouter(prefix="/privacy", tags=["Consent & Privacy Centre"])


class ConsentSet(BaseModel):
    type: ConsentType
    granted: bool
    policy_version: Optional[str] = None


class DataRequestCreate(BaseModel):
    type: DataRequestType
    reason: Optional[str] = Field(None, max_length=2000)


@router.get("/consents", summary="My consent state, one row per consent type")
async def my_consents(user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    stored = {c["type"]: serialize(c) async for c in db.consents.find({"user_id": user.id})}
    return {"consents": [
        stored.get(t.value, {"type": t.value, "granted": False, "granted_at": None,
                             "revoked_at": None, "policy_version": None})
        for t in ConsentType
    ]}


@router.put("/consents", summary="Grant or revoke a consent")
async def set_consent(payload: ConsentSet, request: Request,
                      user: CurrentUser = Depends(get_current_user),
                      db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    update = {
        "user_id": user.id,
        "type": payload.type.value,
        "granted": payload.granted,
        "policy_version": payload.policy_version,
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "updated_at": now,
    }
    update["granted_at" if payload.granted else "revoked_at"] = now
    await db.consents.update_one({"user_id": user.id, "type": payload.type.value},
                                 {"$set": update, "$setOnInsert": {"created_at": now}},
                                 upsert=True)
    await db.consent_history.insert_one({**update, "created_at": now})
    return serialize(await db.consents.find_one({"user_id": user.id,
                                                 "type": payload.type.value}))


@router.get("/consents/history", summary="Full consent audit trail")
async def consent_history(params: PageParams = Depends(page_params),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "consent_history", {"user_id": user.id}, params,
                          sort=[("created_at", -1)])


@router.post("/requests", status_code=http.HTTP_201_CREATED,
             summary="Request a data export, deletion or correction")
async def create_data_request(payload: DataRequestCreate,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    open_same = await db.data_requests.count_documents({
        "user_id": user.id, "type": payload.type.value,
        "status": {"$in": [DataRequestStatus.PENDING.value,
                           DataRequestStatus.IN_PROGRESS.value]}})
    if open_same:
        raise BadRequest(f"You already have an open {payload.type.value} request")

    now = utcnow()
    doc = {
        "user_id": user.id,
        "user_email": user.email,
        "user_name": user.raw.get("full_name"),
        "type": payload.type.value,
        "reason": payload.reason,
        "status": DataRequestStatus.PENDING.value,
        # GDPR gives the controller one month to respond.
        "due_at": now + timedelta(days=30),
        "created_at": now,
        "updated_at": now,
    }
    request_id = str((await db.data_requests.insert_one(doc)).inserted_id)

    owner = await db.users.find_one({"role": Role.CONSULTANT_OWNER.value})
    if owner:
        await notify(db, user_ids=[str(owner["_id"])], type=NotificationType.SUBSCRIPTION,
                     title=f"{payload.type.value.title()} request from {user.raw.get('full_name')}",
                     body="GDPR requires a response within 30 days.",
                     data={"data_request_id": request_id})
    await audit.record(
        action=(AuditAction.DATA_DELETION_REQUESTED if payload.type == DataRequestType.DELETION
                else AuditAction.DATA_EXPORT_REQUESTED),
        actor_id=user.id, actor_email=user.email, tenant_id=user.tenant_id,
        subject=payload.type.value, detail=payload.reason)
    return serialize({**doc, "_id": oid(request_id)})


@router.get("/requests", summary="My data requests")
async def my_data_requests(params: PageParams = Depends(page_params),
                           user: CurrentUser = Depends(get_current_user),
                           db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "data_requests", {"user_id": user.id}, params,
                          sort=[("created_at", -1)])


@router.get("/export", summary="Everything this workspace holds about me, as JSON")
async def export_my_data(user: CurrentUser = Depends(get_current_user),
                         db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """Files are referenced by id, not inlined — download them from /documents/{id}/file."""
    me = await db.users.find_one({"_id": oid(user.id)})
    me = {k: v for k, v in (me or {}).items() if k != "password_hash"}
    scope = {"client_id": user.id}
    return {
        "generated_at": utcnow(),
        "profile": serialize(me),
        "consents": [serialize(c) async for c in db.consents.find({"user_id": user.id})],
        "requests": [serialize(r) async for r in db.requests.find(scope)],
        "cases": [serialize(c) async for c in db.cases.find(scope)],
        "documents": [serialize(d) async for d in db.documents.find(scope)],
        "tasks": [serialize(t) async for t in db.tasks.find({"assignee_id": user.id})],
        "notifications": [serialize(n) async for n in db.notifications.find(
            {"user_id": user.id})],
        "appointments": [serialize(a) async for a in db.appointments.find(
            {"client_id": user.id})],
    }


# ---- consultant side: answering the requests ----
@router.get("/admin/requests", summary="Data requests waiting on this organization")
async def tenant_data_requests(status: Optional[DataRequestStatus] = None,
                               params: PageParams = Depends(page_params),
                               user: CurrentUser = Depends(get_current_user),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    from app.core.exceptions import Forbidden
    if not user.is_consultant:
        raise Forbidden("Consultants only")
    query = {"status": status.value} if status else {}
    return await paginate(db, "data_requests", query, params, sort=[("due_at", 1)])


@router.post("/admin/requests/{request_id}/status", summary="Progress a data request")
async def set_data_request_status(request_id: str, status: DataRequestStatus,
                                  note: Optional[str] = None,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    from app.core.exceptions import Forbidden
    if not user.is_consultant:
        raise Forbidden("Consultants only")
    result = await db.data_requests.find_one_and_update(
        {"_id": oid(request_id)},
        {"$set": {"status": status.value, "handled_by": user.id, "note": note,
                  "updated_at": utcnow()}},
        return_document=True)
    if not result:
        raise NotFound("Data request not found")
    await notify(db, user_ids=[result["user_id"]], type=NotificationType.SUBSCRIPTION,
                 title=f"Your {result['type']} request is {status.value}", body=note or "")
    return serialize(result)
