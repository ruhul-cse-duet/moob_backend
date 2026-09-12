"""The procedure catalogue each organization keeps for itself.

WebImove is not an immigration product. It is the software a consultancy runs
its matters on, and which matters those are is the tenant's business:
immigration for one firm, labour and civil for the next, something nobody has
thought of for the third. The old build had a fixed list of visa types and the
word "immigration" written through every screen, which made the platform usable
by exactly one kind of customer.

Two levels, both per tenant and invisible to every other tenant:

    process area   Immigration, Labour, Civil, Tax, or one they invent
      procedure    Student visa, Dismissal claim, Divorce, Income tax return
                   - and what each needs: documents, client fields, stages

A case is a client plus a procedure the consultant assigned. The client never
picks one.

Procedures are deactivated rather than deleted, and a case copies what it needs
at the moment it is opened. Both exist for the same reason: editing a procedure
must never reach backwards into matters already running on the old version of it.
"""
import re
from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
from app.core.exceptions import BadRequest, Conflict, NotFound
from app.core.utils import oid, serialize, utcnow
from app.modules.catalog.templates import BY_KEY, DEFAULT_AREAS, TEMPLATES


async def ensure_defaults(db) -> None:
    """Give a new workspace the four common areas to start from.

    Seeded rather than hard-coded so they can be renamed, switched off or joined
    by others. A firm that only does tax deactivates the other three; one that
    does something none of these cover adds it.

    Idempotent: run on every tenant provisioning and on first read, because a
    workspace created before the catalogue existed has none of this.
    """
    existing = {row["key"] async for row in db.process_areas.find({}, {"key": 1})}
    missing = [a for a in DEFAULT_AREAS if a["key"] not in existing]
    if not missing:
        return
    now = utcnow()
    await db.process_areas.insert_many([
        {**area, "active": True, "built_in": True, "created_at": now,
         "updated_at": now}
        for area in missing
    ])


# --------------------------------------------------------------------------- #
# Process areas
# --------------------------------------------------------------------------- #


async def list_areas(db, *, include_inactive: bool = False) -> List[Dict[str, Any]]:
    await ensure_defaults(db)
    query: Dict[str, Any] = {} if include_inactive else {"active": True}
    out = []
    async for row in db.process_areas.find(query).sort("name", 1):
        item = serialize(row)
        item["procedure_count"] = await db.procedures.count_documents(
            {"area_key": row["key"], "active": True})
        out.append(item)
    return out


async def create_area(db, user: CurrentUser, data) -> Dict[str, Any]:
    if await db.process_areas.find_one({"key": data.key}):
        raise Conflict(f"This organization already has an area called {data.key!r}")
    now = utcnow()
    doc = {**data.model_dump(), "built_in": False, "created_by": user.id,
           "created_at": now, "updated_at": now}
    result = await db.process_areas.insert_one(doc)
    return serialize({**doc, "_id": result.inserted_id, "procedure_count": 0})


async def update_area(db, area_id: str, data) -> Dict[str, Any]:
    changes = {k: v for k, v in data.model_dump().items() if v is not None}
    if not changes:
        raise BadRequest("Nothing to change")
    changes["updated_at"] = utcnow()

    area = await db.process_areas.find_one({"_id": oid(area_id)})
    if not area:
        raise NotFound("Process area not found")

    # Switching an area off hides it from the pickers; the procedures inside it
    # are left exactly as they are, because cases are still running on them.
    await db.process_areas.update_one({"_id": oid(area_id)}, {"$set": changes})
    updated = await db.process_areas.find_one({"_id": oid(area_id)})
    out = serialize(updated)
    out["procedure_count"] = await db.procedures.count_documents(
        {"area_key": updated["key"], "active": True})
    return out


# --------------------------------------------------------------------------- #
# Procedures
# --------------------------------------------------------------------------- #


async def _area_names(db) -> Dict[str, str]:
    return {row["key"]: row.get("name", row["key"])
            async for row in db.process_areas.find({}, {"key": 1, "name": 1})}


async def list_procedures(db, *, area_key: Optional[str] = None,
                          include_inactive: bool = False) -> List[Dict[str, Any]]:
    await ensure_defaults(db)
    query: Dict[str, Any] = {}
    if area_key:
        query["area_key"] = area_key
    if not include_inactive:
        query["active"] = True

    names = await _area_names(db)
    out = []
    async for row in db.procedures.find(query).sort("name", 1):
        item = serialize(row)
        item["area_name"] = names.get(row.get("area_key"))
        out.append(item)
    return out


async def get_procedure(db, procedure_id: str) -> Dict[str, Any]:
    row = await db.procedures.find_one({"_id": oid(procedure_id)})
    if not row:
        raise NotFound("Procedure not found")
    out = serialize(row)
    out["area_name"] = (await _area_names(db)).get(row.get("area_key"))
    return out


async def _assert_area_exists(db, area_key: str) -> None:
    if not await db.process_areas.find_one({"key": area_key}):
        raise BadRequest(
            f"No process area {area_key!r} in this organization. "
            f"Create the area first, or pick an existing one."
        )


async def create_procedure(db, user: CurrentUser, data,
                           from_template: Optional[str] = None) -> Dict[str, Any]:
    await ensure_defaults(db)
    await _assert_area_exists(db, data.area_key)
    if await db.procedures.find_one({"area_key": data.area_key, "name": data.name}):
        raise Conflict(f"A procedure called {data.name!r} already exists in this area")

    now = utcnow()
    doc = {**data.model_dump(), "from_template": from_template,
           "created_by": user.id, "created_at": now, "updated_at": now}
    result = await db.procedures.insert_one(doc)
    return await get_procedure(db, str(result.inserted_id))


async def update_procedure(db, procedure_id: str, data) -> Dict[str, Any]:
    row = await db.procedures.find_one({"_id": oid(procedure_id)})
    if not row:
        raise NotFound("Procedure not found")

    changes = {k: v for k, v in data.model_dump(exclude_unset=True).items()
               if v is not None}
    if not changes:
        raise BadRequest("Nothing to change")
    if "area_key" in changes:
        await _assert_area_exists(db, changes["area_key"])

    # Editing a procedure changes what the *next* case built on it looks like.
    # Cases already open carry their own copy of the checklist, so none of this
    # reaches them - which is the whole reason they carry a copy.
    changes["updated_at"] = utcnow()
    await db.procedures.update_one({"_id": oid(procedure_id)}, {"$set": changes})
    return await get_procedure(db, procedure_id)


async def duplicate_procedure(db, user: CurrentUser, procedure_id: str,
                              name: Optional[str] = None) -> Dict[str, Any]:
    """Copy one to edit, which is how a variant gets made without risking the
    original that cases are already running on."""
    row = await db.procedures.find_one({"_id": oid(procedure_id)})
    if not row:
        raise NotFound("Procedure not found")

    copy = {k: v for k, v in row.items()
            if k not in {"_id", "created_at", "updated_at", "created_by"}}
    copy["name"] = name or _next_copy_name(row["name"])
    if await db.procedures.find_one({"area_key": copy["area_key"],
                                     "name": copy["name"]}):
        raise Conflict(f"A procedure called {copy['name']!r} already exists")

    now = utcnow()
    copy.update({"created_by": user.id, "created_at": now, "updated_at": now})
    result = await db.procedures.insert_one(copy)
    return await get_procedure(db, str(result.inserted_id))


def _next_copy_name(name: str) -> str:
    base = re.sub(r"\s*\(copy(?: \d+)?\)$", "", name)
    return f"{base} (copy)"


async def deactivate_procedure(db, procedure_id: str) -> Dict[str, Any]:
    """Retire a procedure without deleting it.

    Deleting would orphan every case that names it - the case would lose the
    record of what it is, which is not an improvement on a procedure nobody
    picks any more. Deactivated ones disappear from the picker and stay readable
    to anything that already points at them.
    """
    row = await db.procedures.find_one({"_id": oid(procedure_id)})
    if not row:
        raise NotFound("Procedure not found")
    await db.procedures.update_one(
        {"_id": oid(procedure_id)},
        {"$set": {"active": False, "updated_at": utcnow()}})
    return await get_procedure(db, procedure_id)


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def list_templates(area_key: Optional[str] = None) -> List[Dict[str, Any]]:
    if area_key:
        return [t for t in TEMPLATES if t["area_key"] == area_key]
    return list(TEMPLATES)


async def create_from_template(db, user: CurrentUser, template_key: str,
                               name: Optional[str] = None) -> Dict[str, Any]:
    template = BY_KEY.get(template_key)
    if not template:
        raise NotFound(f"No template {template_key!r}")

    await ensure_defaults(db)
    await _assert_area_exists(db, template["area_key"])

    class _Data:
        """The template, shaped like the create payload."""
        area_key = template["area_key"]
        active = True

        def model_dump(self):
            return {
                "area_key": template["area_key"],
                "name": name or template["name"],
                "description": template.get("description"),
                "required_documents": template.get("required_documents", []),
                "client_fields": template.get("client_fields", []),
                "workflow_stages": template.get("workflow_stages", []),
                "default_deadline_days": template.get("default_deadline_days"),
                "active": True,
            }

    data = _Data()
    data.name = name or template["name"]
    return await create_procedure(db, user, data, from_template=template_key)


# --------------------------------------------------------------------------- #
# Used by requests and cases
# --------------------------------------------------------------------------- #


async def snapshot_for_case(db, procedure_id: str) -> Dict[str, Any]:
    """What a case copies out of a procedure when it is opened.

    A copy, not a reference, so that editing the procedure next month does not
    silently rewrite the checklist a case was assessed against. `procedure_id`
    is kept beside it for provenance - which procedure this came from - but
    nothing reads through it to decide what the case requires.
    """
    row = await db.procedures.find_one({"_id": oid(procedure_id)})
    if not row:
        raise NotFound("Procedure not found")
    return {
        "procedure_id": str(row["_id"]),
        "procedure_name": row.get("name"),
        "process_area": row.get("area_key"),
        "required_documents": row.get("required_documents", []),
        "client_fields": row.get("client_fields", []),
        "workflow_stages": row.get("workflow_stages", []),
        "default_deadline_days": row.get("default_deadline_days"),
    }
