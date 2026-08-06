"""
legal/TermsOfService.tsx, legal/PrivacyPolicy.tsx, legal/PolicyPage.tsx,
PlatformDocs.tsx  [INFERRED]

Policies are versioned platform-wide and served publicly (signup has to show
them before anyone has an account). Acceptance is recorded per email so it
survives the user moving between organizations.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status as http
from pydantic import BaseModel, Field

from app.core.deps import CurrentUser, get_current_user, page_params, require_super_admin
from app.core.enums import PolicyKind
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.schemas.common import PageParams
from app.services.pagination import paginate

router = APIRouter(prefix="/legal", tags=["Legal & Policies"])


class PolicyCreate(BaseModel):
    kind: PolicyKind
    version: str = Field(examples=["2.0"])
    title: str
    body_markdown: str
    effective_from: Optional[str] = None
    publish: bool = False


@router.get("/policies", summary="Currently published policies (public)")
async def published_policies(kind: Optional[PolicyKind] = Query(None)):
    db = platform_db()
    kinds = [kind] if kind else list(PolicyKind)
    out = []
    for k in kinds:
        doc = await db.policies.find_one({"kind": k.value, "published": True},
                                         sort=[("version", -1)])
        if doc:
            out.append(serialize(doc))
    return {"policies": out}


@router.get("/policies/{kind}", summary="One published policy by kind (public)")
async def policy(kind: PolicyKind):
    doc = await platform_db().policies.find_one({"kind": kind.value, "published": True},
                                                sort=[("version", -1)])
    if not doc:
        raise NotFound(f"No published {kind.value}")
    return serialize(doc)


@router.post("/policies/{kind}/accept", summary="Record that I accepted this policy")
async def accept_policy(kind: PolicyKind, version: Optional[str] = Query(None),
                        user: CurrentUser = Depends(get_current_user)):
    db = platform_db()
    if not version:
        current = await db.policies.find_one({"kind": kind.value, "published": True},
                                             sort=[("version", -1)])
        version = current["version"] if current else "1.0"
    now = utcnow()
    await db.policy_acceptances.update_one(
        {"user_email": user.email, "kind": kind.value},
        {"$set": {"version": version, "user_id": user.id, "tenant_id": user.tenant_id,
                  "accepted_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True)
    return {"kind": kind.value, "version": version, "accepted_at": now}


@router.get("/acceptances", summary="What I have accepted, and whether it is current")
async def my_acceptances(user: CurrentUser = Depends(get_current_user)):
    db = platform_db()
    rows = []
    async for k in _kinds():
        accepted = await db.policy_acceptances.find_one({"user_email": user.email,
                                                         "kind": k.value})
        current = await db.policies.find_one({"kind": k.value, "published": True},
                                             sort=[("version", -1)])
        rows.append({
            "kind": k.value,
            "accepted_version": (accepted or {}).get("version"),
            "accepted_at": (accepted or {}).get("accepted_at"),
            "current_version": (current or {}).get("version"),
            "up_to_date": bool(accepted and current
                               and accepted.get("version") == current.get("version")),
        })
    return {"items": rows}


async def _kinds():
    for k in PolicyKind:
        yield k


# ---- platform admin authoring ----
@router.post("/admin/policies", status_code=http.HTTP_201_CREATED,
             summary="Publish a new policy version (super admin)")
async def create_policy(payload: PolicyCreate,
                        user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    now = utcnow()
    if payload.publish:
        await db.policies.update_many({"kind": payload.kind.value},
                                      {"$set": {"published": False}})
    doc = {**payload.model_dump(exclude={"publish"}),
           "kind": payload.kind.value,
           "published": payload.publish,
           "created_by": user.id,
           "created_at": now,
           "updated_at": now}
    policy_id = str((await db.policies.insert_one(doc)).inserted_id)
    return serialize({**doc, "_id": oid(policy_id)})


@router.get("/admin/policies", summary="All policy versions (super admin)")
async def list_policies(kind: Optional[PolicyKind] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_super_admin)):
    query = {"kind": kind.value} if kind else {}
    return await paginate(platform_db(), "policies", query, params,
                          sort=[("kind", 1), ("version", -1)])


@router.post("/admin/policies/{policy_id}/publish",
             summary="Make one version the live policy (super admin)")
async def publish_policy(policy_id: str, user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    doc = await db.policies.find_one({"_id": oid(policy_id)})
    if not doc:
        raise NotFound("Policy version not found")
    await db.policies.update_many({"kind": doc["kind"]}, {"$set": {"published": False}})
    await db.policies.update_one({"_id": oid(policy_id)},
                                 {"$set": {"published": True, "published_at": utcnow()}})
    return serialize(await db.policies.find_one({"_id": oid(policy_id)}))
