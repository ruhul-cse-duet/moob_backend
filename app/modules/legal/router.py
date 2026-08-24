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
from app.core.enums import AuditAction, PolicyKind
from app.core.exceptions import Conflict, NotFound
from app.core.utils import oid, serialize, utcnow
from app.db.mongo import platform_db
from app.schemas.common import Message, PageParams
from app.services import audit
from app.services.pagination import paginate

router = APIRouter(prefix="/legal", tags=["Legal & Policies"])


class PolicyCreate(BaseModel):
    kind: PolicyKind
    version: str = Field(examples=["2.0"])
    title: str
    body_markdown: str
    effective_from: Optional[str] = None
    publish: bool = False


class PolicyUpdate(BaseModel):
    """Edit a draft. Every field optional - the editor sends what changed."""

    version: Optional[str] = Field(None, examples=["2.1"])
    title: Optional[str] = Field(None, min_length=1)
    body_markdown: Optional[str] = Field(None, min_length=1)
    effective_from: Optional[str] = None


async def _assert_version_free(kind: str, version: str,
                               ignoring: Optional[str] = None) -> None:
    query = {"kind": kind, "version": version}
    if ignoring:
        query["_id"] = {"$ne": oid(ignoring)}
    if await platform_db().policies.find_one(query, {"_id": 1}):
        raise Conflict(f"Version {version} of this policy already exists")


async def _record(user: CurrentUser, doc: dict, what: str) -> None:
    """Policy changes are legally significant, so they go in the audit trail."""
    await audit.record(
        action=AuditAction.ADMIN_ACTION, actor_id=user.id, actor_email=user.email,
        actor_role="super_admin", subject=f"{doc['kind']} {doc.get('version', '')}".strip(),
        detail=what,
    )


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
    await _assert_version_free(payload.kind.value, payload.version)

    doc = {**payload.model_dump(exclude={"publish"}),
           "kind": payload.kind.value,
           "published": payload.publish,
           "created_by": user.id,
           "created_at": now,
           "updated_at": now}
    if payload.publish:
        doc["published_at"] = now
    policy_id = (await db.policies.insert_one(doc)).inserted_id

    # Insert first, retire the others second. The old order cleared every
    # published flag *before* the new row existed, so for that instant the
    # public endpoint had no policy of this kind to serve - and signup has to
    # show the terms before anyone has an account.
    if payload.publish:
        await db.policies.update_many(
            {"kind": payload.kind.value, "_id": {"$ne": policy_id}},
            {"$set": {"published": False}},
        )

    await _record(user, doc, "published a new version" if payload.publish
                  else "created a draft version")
    return serialize({**doc, "_id": policy_id})


@router.get("/admin/policies", summary="All policy versions (super admin)")
async def list_policies(kind: Optional[PolicyKind] = Query(None),
                        params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_super_admin)):
    query = {"kind": kind.value} if kind else {}
    return await paginate(platform_db(), "policies", query, params,
                          sort=[("kind", 1), ("version", -1)])


@router.get("/admin/policies/{policy_id}",
            summary="One policy version, for the editor (super admin)")
async def get_policy_version(policy_id: str,
                             user: CurrentUser = Depends(require_super_admin)):
    doc = await platform_db().policies.find_one({"_id": oid(policy_id)})
    if not doc:
        raise NotFound("Policy version not found")
    return serialize(doc)


@router.patch("/admin/policies/{policy_id}",
              summary="Edit an unpublished draft (super admin)")
async def update_policy(policy_id: str, payload: PolicyUpdate,
                        user: CurrentUser = Depends(require_super_admin)):
    """Change the wording of a draft before it goes live.

    A **published** version is immutable here, and deliberately so. Acceptance
    is recorded as a version string, and `/legal/acceptances` decides
    `up_to_date` by comparing that string with the live one. Rewriting the body
    of a version people have already accepted would leave those records saying
    they agreed to text they never saw, and the platform would report everyone
    as up to date. Use `/duplicate` to start a new version instead.
    """
    db = platform_db()
    doc = await db.policies.find_one({"_id": oid(policy_id)})
    if not doc:
        raise NotFound("Policy version not found")
    if doc.get("published"):
        raise Conflict(
            "A published version cannot be edited, because people have already "
            "accepted it. Duplicate it to a new draft, edit that, and publish."
        )

    sent = payload.model_fields_set
    updates = {f: getattr(payload, f) for f in
               ("version", "title", "body_markdown", "effective_from") if f in sent}
    if not updates:
        return serialize(doc)

    if "version" in updates and updates["version"] != doc.get("version"):
        await _assert_version_free(doc["kind"], updates["version"], ignoring=policy_id)

    updates["updated_at"] = utcnow()
    await db.policies.update_one({"_id": doc["_id"]}, {"$set": updates})
    await _record(user, {**doc, **updates}, "edited a draft version")
    return serialize(await db.policies.find_one({"_id": doc["_id"]}))


@router.post("/admin/policies/{policy_id}/duplicate", status_code=http.HTTP_201_CREATED,
             summary="Start a new draft from an existing version (super admin)")
async def duplicate_policy(policy_id: str, version: str = Query(min_length=1),
                           user: CurrentUser = Depends(require_super_admin)):
    """Copy a version's text into a new, unpublished draft.

    This is how the live Privacy Policy or Terms actually get updated: the
    current text is copied forward, edited as a draft, then published - so the
    version people accepted stays exactly as it was.
    """
    db = platform_db()
    source = await db.policies.find_one({"_id": oid(policy_id)})
    if not source:
        raise NotFound("Policy version not found")
    await _assert_version_free(source["kind"], version)

    now = utcnow()
    doc = {
        "kind": source["kind"],
        "version": version,
        "title": source.get("title"),
        "body_markdown": source.get("body_markdown"),
        "effective_from": None,
        "published": False,
        "duplicated_from": str(source["_id"]),
        "created_by": user.id,
        "created_at": now,
        "updated_at": now,
    }
    doc["_id"] = (await db.policies.insert_one(doc)).inserted_id
    await _record(user, doc, f"started a draft from version {source.get('version')}")
    return serialize(doc)


@router.delete("/admin/policies/{policy_id}", response_model=Message,
               summary="Discard an unpublished draft (super admin)")
async def delete_policy(policy_id: str,
                        user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    doc = await db.policies.find_one({"_id": oid(policy_id)})
    if not doc:
        raise NotFound("Policy version not found")
    if doc.get("published"):
        raise Conflict(
            "The live policy cannot be deleted. Publish another version first."
        )
    await db.policies.delete_one({"_id": doc["_id"]})
    await _record(user, doc, "discarded a draft version")
    return {"detail": f"Draft {doc.get('version')} discarded"}


@router.post("/admin/policies/{policy_id}/publish",
             summary="Make one version the live policy (super admin)")
async def publish_policy(policy_id: str, user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    doc = await db.policies.find_one({"_id": oid(policy_id)})
    if not doc:
        raise NotFound("Policy version not found")

    # Live one first, then retire the rest - never the other way round, or this
    # policy kind is unpublished for the moment in between.
    await db.policies.update_one({"_id": doc["_id"]},
                                 {"$set": {"published": True, "published_at": utcnow()}})
    await db.policies.update_many(
        {"kind": doc["kind"], "_id": {"$ne": doc["_id"]}},
        {"$set": {"published": False}},
    )
    await _record(user, doc, "made this version live")
    return serialize(await db.policies.find_one({"_id": doc["_id"]}))
