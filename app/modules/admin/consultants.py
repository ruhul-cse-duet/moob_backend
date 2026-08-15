from typing import Optional

from fastapi import APIRouter, Depends, Query, status as http
from pydantic import BaseModel

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import Role, UserStatus
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db, tenant_db
from app.schemas.common import Message, PageParams

router = APIRouter(prefix="/users", tags=["Super Admin · Consultant Management"])

class ConsultantStatusUpdate(BaseModel):
    status: str # "active" or "suspended"

@router.get("", summary="List consultants across the platform")
async def list_consultants(
    tab: Optional[str] = Query("all", description="all | active | disabled"),
    search: Optional[str] = Query(None),
    params: PageParams = Depends(page_params),
    user: CurrentUser = Depends(require_super_admin)
):
    db = platform_db()
    
    # 1. Fetch all consultant refs from user_directory
    cursor = db.user_directory.find(
        {"role": {"$in": [Role.CONSULTANT_OWNER.value, Role.CONSULTANT.value]}}
    )
    
    results = []
    # Optimization: Cache tenant names to avoid redundant queries
    tenant_names = {}
    
    async for d_user in cursor:
        tid = d_user.get("tenant_id")
        uid = d_user.get("user_id")
        if not tid or not uid:
            continue
            
        tdb = tenant_db(tid)
        t_user = await tdb.users.find_one({"_id": oid(uid)})
        if not t_user:
            continue
            
        # Get tenant name
        if tid not in tenant_names:
            t = await db.tenants.find_one({"_id": oid(tid)})
            tenant_names[tid] = t["name"] if t else "Unknown Organization"
            
        # Construct unified object
        user_status = t_user.get("status", UserStatus.ACTIVE.value)
        
        # In UI, "suspended" might be mapped to "disabled"
        is_disabled = user_status == UserStatus.SUSPENDED.value
        mapped_status = "Disabled" if is_disabled else "Active"
        
        # Filter by tab
        if tab == "active" and is_disabled:
            continue
        if tab == "disabled" and not is_disabled:
            continue
            
        full_name = t_user.get("full_name", "")
        email = d_user.get("email", "")
        
        # Filter by search
        if search:
            search_lower = search.lower()
            if search_lower not in full_name.lower() and search_lower not in email.lower():
                continue
                
        results.append({
            "id": str(t_user["_id"]),
            "tenant_id": tid,
            "full_name": full_name,
            "email": email,
            "role": "Consultant",
            "title": t_user.get("title", "Consultant"),
            "organization_name": tenant_names[tid],
            "status": mapped_status,
            "created_at": d_user.get("created_at"),
            "raw_status": user_status
        })

    # Sort results newest first
    results.sort(key=lambda x: x.get("created_at") or utcnow(), reverse=True)
    
    # In-memory pagination
    total = len(results)
    pages = (total + params.page_size - 1) // params.page_size
    items = results[params.skip : params.skip + params.page_size]
    
    return {
        "success": True,
        "message": "OK",
        "items": items,
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "pages": pages,
    }


@router.patch("/{tenant_id}/{user_id}/status", summary="Disable or enable a consultant account")
async def update_consultant_status(
    tenant_id: str, 
    user_id: str, 
    payload: ConsultantStatusUpdate,
    admin: CurrentUser = Depends(require_super_admin)
):
    db = platform_db()
    tdb = tenant_db(tenant_id)
    
    t_user = await tdb.users.find_one({"_id": oid(user_id)})
    if not t_user:
        raise NotFound("Consultant not found in organization")
        
    new_status = UserStatus.SUSPENDED.value if payload.status.lower() in ["disabled", "suspended"] else UserStatus.ACTIVE.value
    
    # Update in tenant DB
    await tdb.users.update_one(
        {"_id": oid(user_id)},
        {"$set": {"status": new_status, "updated_at": utcnow()}}
    )
    
    return {"success": True, "message": f"Account status updated to {new_status}"}
