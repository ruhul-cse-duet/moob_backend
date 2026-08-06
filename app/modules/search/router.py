from fastapi import APIRouter, Depends, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    require_active_tenant,
)
from app.core.enums import Role
from app.core.utils import serialize

router = APIRouter(tags=["Search"], dependencies=[Depends(require_active_tenant)])


@router.get("/search", summary="Search clients, cases, requests and documents")
async def search(q: str = Query(min_length=2), limit: int = Query(5, ge=1, le=20),
                 user: CurrentUser = Depends(get_current_user),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    rx = {"$regex": q, "$options": "i"}
    client_scope = {"client_id": user.id} if user.role == Role.CLIENT else {}

    clients = []
    if user.role != Role.CLIENT:
        clients = [serialize({k: v for k, v in d.items() if k != "password_hash"})
                   async for d in db.users.find(
                       {"role": Role.CLIENT.value,
                        "$or": [{"full_name": rx}, {"email": rx}]}).limit(limit)]

    cases = [serialize(d) async for d in db.cases.find(
        {**client_scope, "$or": [{"reference": rx}, {"client_name": rx},
                                 {"case_type": rx}]}).limit(limit)]
    requests = [serialize(d) async for d in db.requests.find(
        {**client_scope, "$or": [{"reference": rx}, {"client_name": rx},
                                 {"visa_type": rx}]}).limit(limit)]
    documents = [serialize(d) async for d in db.documents.find(
        {**client_scope, "name": rx}).limit(limit)]

    return {"query": q, "clients": clients, "cases": cases,
            "requests": requests, "documents": documents}
