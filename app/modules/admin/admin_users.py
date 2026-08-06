"""admin/AdminUsers.tsx — platform staff accounts.  [INFERRED]"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status as http
from pydantic import BaseModel, EmailStr, Field

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import AdminRole, AuditAction, UserStatus
from app.core.exceptions import Conflict, NotFound
from app.core.security import hash_password
from app.core.utils import oid, random_token, serialize, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message, PageParams
from app.services import audit
from app.services.email import send_email
from app.services.pagination import paginate

router = APIRouter(prefix="/users", tags=["Super Admin · Platform staff"])


class AdminUserCreate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    admin_role: AdminRole = AdminRole.SUPPORT_AGENT


class AdminUserUpdate(BaseModel):
    full_name: Optional[str] = None
    admin_role: Optional[AdminRole] = None
    status: Optional[UserStatus] = None


@router.get("", summary="Platform staff directory")
async def list_admins(admin_role: Optional[AdminRole] = Query(None),
                      search: Optional[str] = Query(None),
                      params: PageParams = Depends(page_params),
                      user: CurrentUser = Depends(require_super_admin)):
    query = {}
    if admin_role:
        query["admin_role"] = admin_role.value
    if search:
        query["$or"] = [{"full_name": {"$regex": search, "$options": "i"}},
                        {"email": {"$regex": search, "$options": "i"}}]
    page = await paginate(platform_db(), "platform_admins", query, params,
                          sort=[("created_at", -1)])
    for item in page["items"]:
        item.pop("password_hash", None)
    return page


@router.post("", status_code=http.HTTP_201_CREATED,
             summary="Invite a platform staff member")
async def create_admin(payload: AdminUserCreate,
                       user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    email = payload.email.lower()
    if await db.platform_admins.find_one({"email": email}):
        raise Conflict("This staff account already exists")

    temp = random_token(12)
    now = utcnow()
    doc = {
        "email": email,
        "full_name": payload.full_name,
        "admin_role": payload.admin_role.value,
        "password_hash": hash_password(temp),
        "must_change_password": True,
        "status": UserStatus.ACTIVE.value,
        "created_by": user.id,
        "created_at": now,
        "updated_at": now,
    }
    admin_id = str((await db.platform_admins.insert_one(doc)).inserted_id)
    await send_email(
        to=email,
        subject="Your WebImove platform account",
        html=f"<p>Hi {payload.full_name}, an account was created for you with role "
             f"<strong>{payload.admin_role.value}</strong>.</p>"
             f"<p>Temporary password: <code>{temp}</code> — change it on first sign-in.</p>",
    )
    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject="created platform staff",
                       detail=email)
    doc.pop("password_hash")
    return serialize({**doc, "_id": oid(admin_id)})


@router.patch("/{admin_id}", summary="Change a staff member's role or status")
async def update_admin(admin_id: str, payload: AdminUserUpdate,
                       user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    data = {k: (v.value if hasattr(v, "value") else v)
            for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    data["updated_at"] = utcnow()
    result = await db.platform_admins.find_one_and_update(
        {"_id": oid(admin_id)}, {"$set": data}, return_document=True)
    if not result:
        raise NotFound("Staff account not found")
    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject="updated platform staff",
                       detail=result["email"], meta=data)
    result.pop("password_hash", None)
    return serialize(result)


@router.delete("/{admin_id}", response_model=Message)
async def delete_admin(admin_id: str, user: CurrentUser = Depends(require_super_admin)):
    if admin_id == user.id:
        raise Conflict("You cannot delete your own account")
    result = await platform_db().platform_admins.delete_one({"_id": oid(admin_id)})
    if not result.deleted_count:
        raise NotFound("Staff account not found")
    await audit.record(action=AuditAction.ADMIN_ACTION, actor_id=user.id,
                       actor_email=user.email, subject="deleted platform staff")
    return {"detail": "Staff account removed"}
