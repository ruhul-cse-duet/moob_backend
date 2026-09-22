from typing import Any, Dict, List, Optional

from app.core.deps import CurrentUser
from app.core.i18n import DEFAULT_LANGUAGE, translate
from app.core.enums import (
    CASE_STAGE_ORDER,
    CaseStage,
    NotificationType,
    Role,
    TaskAssigneeType,
    TaskStatus,
)
from app.core.exceptions import BadRequest, Forbidden, NotFound
from app.core.utils import build_reference, oid, serialize, utcnow
from app.db.indexes import next_sequence
from app.modules.deadlines import service as deadlines
from app.schemas.common import PageParams
from app.services.case_progress import APPROVED, apply_progress, attach_case_progress
from app.services.events import log_activity, notify
from app.services.ai_service import case_guidance, fill_case_form
from app.services.ownership import assert_case_access, assigned_client_ids, attach_consultant
from app.services.pagination import paginate

#: Every static string a `log_activity(action=...)` call anywhere in the app
#: passes for a case-scoped event, mapped to its translation key. Kept here
#: rather than beside each call site because this is the one place that reads
#: them back - `case_history()` - and a label added there with no call site
#: writing it yet would never fire, the same trap a scattered mapping invites.
_ACTIVITY_LABEL_KEYS = {
    "approved": "activity.approved",
    "approved a request": "activity.approved_request",
    "assigned a task": "activity.assigned_task",
    "declined a partner task": "activity.declined_partner_task",
    "completed a partner task": "activity.completed_partner_task",
    "delivered a partner task": "activity.delivered_partner_task",
    "completed the consultation for": "activity.completed_consultation",
    "declined a request": "activity.declined_request",
    "opened a case for": "activity.opened_case",
    "opened a request": "activity.opened_request",
    "removed the document request": "activity.removed_document_request",
    "requested documents for": "activity.requested_documents",
    "returned to the client": "activity.returned_to_client",
    "submitted a request": "activity.submitted_request",
    "uploaded": "activity.uploaded",
    "withdrew a request": "activity.withdrew_request",
    "logged an authority request for": "activity.authority_request_logged",
}
_ADVANCED_PREFIX = "advanced to "


def _stage_order(case: Dict[str, Any]) -> List[str]:
    """The ordered list of stage keys this case actually moves through.

    A case opened against a procedure moves through *that procedure's own*
    stages - a civil matter and an immigration case do not share a workflow,
    and forcing both through the platform's fixed 11-stage immigration ladder
    is exactly what review item C1 flagged: a case opened with no documents,
    "no deadline" and stage names like "government submission" no matter what
    was actually assigned. A case with no procedure (or one opened before
    procedures existed) still gets the fixed ladder, so nothing already
    running on it breaks.
    """
    custom = case.get("workflow_stages") or []
    if custom:
        return [s["key"] for s in custom]
    return [st.value for st in CASE_STAGE_ORDER]


def _stage_label(case: Dict[str, Any], stage_key: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """A procedure's stage name is the consultant's own wording (typed into
    the Procedure Catalog), so - like a document name - it is shown exactly as
    written, never run through the platform's translation catalogue. Only the
    fixed fallback ladder's stages are, since those are the platform's own
    words and the only ones a translation exists for.
    """
    for s in (case.get("workflow_stages") or []):
        if s.get("key") == stage_key:
            return s.get("name") or stage_key
    return translate(f"stage.{stage_key}", lang)


def _progress_for(case: Dict[str, Any], stage_key: str) -> int:
    order = _stage_order(case)
    if stage_key not in order:
        return case.get("progress", 0) or 0
    idx = order.index(stage_key)
    return round((idx / max(len(order) - 1, 1)) * 100)


async def _get(db, case_id: str) -> Dict[str, Any]:
    doc = await db.cases.find_one({"_id": oid(case_id)})
    if not doc:
        raise NotFound("Case not found")
    return doc


async def create_case(db, user: CurrentUser, data) -> Dict[str, Any]:
    seq = await next_sequence(db, "case", start=80)
    client = await db.users.find_one({"_id": oid(data.client_id)})
    if not client:
        raise NotFound("Client not found")

    # A copy of the procedure, not a pointer to it. Editing the catalogue next
    # month must not rewrite the checklist this case was assessed against.
    procedure = {}
    if getattr(data, "procedure_id", None):
        from app.modules.catalog import service as catalog

        procedure = await catalog.snapshot_for_case(db, data.procedure_id)

    consultant_id = client.get("consultant_id") or user.id

    # Same decision, same place, as opening a case from a request.
    from app.modules.catalog import service as catalog_plan

    plan = catalog_plan.case_plan(procedure, data.deadline)
    workflow_stages = plan["workflow_stages"]
    initial_stage = plan["stage"] or CaseStage.NEW_REQUEST.value
    deadline = plan["deadline"]

    now = utcnow()
    doc = {
        "reference": build_reference("CAS", seq),
        "request_id": data.request_id,
        "client_id": data.client_id,
        "client_name": client.get("full_name"),
        # The client's owning consultant keeps the case unless one is acting directly.
        "consultant_id": consultant_id,
        "process_area": procedure.get("process_area")
                        or getattr(data, "process_area", None),
        "procedure_id": procedure.get("procedure_id"),
        "procedure_name": procedure.get("procedure_name"),
        "required_documents": procedure.get("required_documents", []),
        "client_fields": procedure.get("client_fields", []),
        "workflow_stages": workflow_stages,
        "case_type": data.case_type or procedure.get("procedure_name"),
        "destination_country": data.destination_country,
        "stage": initial_stage,
        "progress": 0,
        "deadline": deadline,
        "timeline": [{"stage": initial_stage, "at": now, "by": user.id}],
        "ai_guidance": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.cases.insert_one(doc)
    case_id = str(result.inserted_id)

    # Bind the request and its documents to the new case, the same way
    # completing a consultation does. Without this the documents keep a null
    # `case_id`, and anything scoped by case - a delegated partner's access
    # among it - cannot see them.
    if data.request_id:
        await db.requests.update_one(
            {"_id": oid(data.request_id), "case_id": None},
            {"$set": {"case_id": case_id, "updated_at": now}},
        )
        await db.documents.update_many(
            {"request_id": data.request_id},
            {"$set": {"case_id": case_id, "updated_at": now}},
        )

    # The other half of C1: a procedure names the documents a case needs, but
    # nothing ever turned that list into actual document requests - the case
    # opened with an empty checklist and the client had nothing to upload
    # until the consultant separately asked. This is what
    # `requests/service.py::request_documents` does for a request, done here
    # for a case at the moment its procedure is assigned.
    from app.modules.documents.service import _sync_request_status

    required_documents = procedure.get("required_documents") or []
    if required_documents:
        from app.core.enums import DocumentCategory, DocumentStatus

        valid_categories = {c.value for c in DocumentCategory}
        inserts = []
        for item in required_documents:
            category = str(item.get("category") or "other").lower()
            if category not in valid_categories:
                # A template's own category ("supporting", "legal") is a
                # consultant's word, not one of the platform's four - see the
                # note on `RequiredDocument.category`. Coerced rather than
                # rejected, the same way the frontend already treats an
                # unrecognised category when a consultant requests documents
                # by hand.
                category = DocumentCategory.OTHER.value
            inserts.append({
                "request_id": data.request_id,
                "case_id": case_id,
                "client_id": data.client_id,
                "consultant_id": consultant_id,
                "name": item.get("name", ""),
                "category": category,
                "why": item.get("why"),
                "is_required": bool(item.get("mandatory", True)),
                "status": DocumentStatus.UPLOAD_NEEDED.value,
                "due_date": deadline,
                "file": None,
                "ai_analysis": None,
                "consultant_feedback": None,
                "requested_by": user.id,
                "created_at": now,
                "updated_at": now,
            })
        insert_result = await db.documents.insert_many(inserts)
        for item, document_id in zip(inserts, insert_result.inserted_ids):
            await deadlines.upsert(
                db, kind="document_request", source_collection="documents",
                source_id=str(document_id), due_date=item["due_date"], title=item["name"],
                consultant_id=consultant_id, client_id=data.client_id,
                client_name=client.get("full_name"), case_id=case_id,
                case_reference=doc["reference"],
            )
        if data.request_id:
            await _sync_request_status(db, {"request_id": data.request_id})

    return await attach_consultant(db, serialize({**doc, "_id": result.inserted_id}))


async def list_cases(db, user: CurrentUser, params: PageParams,
                     stage: Optional[CaseStage] = None,
                     search: Optional[str] = None,
                     consultant_id: Optional[str] = None,
                     lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    query: Dict[str, Any] = {}
    if consultant_id:
        query["consultant_id"] = consultant_id
    if user.role == Role.CLIENT:
        query["client_id"] = user.id
    elif user.role == Role.PARTNER:
        # A partner sees cases from two sources: clients bulk-assigned to them
        # ("process this client like a consultant") and any case they still have
        # an individual delegated task on (the older, per-case delegation model).
        client_ids = await assigned_client_ids(db, user.id)
        task_case_ids = await db.tasks.distinct("case_id", {"assignee_id": user.id})
        query["$or"] = [
            {"client_id": {"$in": client_ids}},
            {"_id": {"$in": [oid(c) for c in task_case_ids if c]}},
        ]
    if stage:
        query["stage"] = stage.value
    if search:
        search_or = [
            {"reference": {"$regex": search, "$options": "i"}},
            {"client_name": {"$regex": search, "$options": "i"}},
            {"case_type": {"$regex": search, "$options": "i"}},
        ]
        if "$or" in query:
            query["$and"] = [{"$or": query.pop("$or")}, {"$or": search_or}]
        else:
            query["$or"] = search_or
    page = await paginate(db, "cases", query, params,
                          sort=[("deadline", 1), ("created_at", -1)])
    # Progress is derived from documents, so a list has to say the same thing
    # the detail screen does - one aggregation for the page, not two per case.
    page["items"] = await attach_case_progress(db, page["items"])
    for item in page["items"]:
        if "stage" in item:
            item["stage_label"] = _stage_label(item, item["stage"], lang)
    return page


async def stage_counts(db, user: CurrentUser,
                       consultant_id: Optional[str] = None) -> Dict[str, int]:
    base: Dict[str, Any] = {}
    if user.role == Role.CLIENT:
        base["client_id"] = user.id
    elif user.role == Role.PARTNER:
        client_ids = await assigned_client_ids(db, user.id)
        task_case_ids = await db.tasks.distinct("case_id", {"assignee_id": user.id})
        base["$or"] = [
            {"client_id": {"$in": client_ids}},
            {"_id": {"$in": [oid(c) for c in task_case_ids if c]}},
        ]
    if consultant_id:
        base["consultant_id"] = consultant_id
    out = {"all": await db.cases.count_documents(base)}
    for st in CASE_STAGE_ORDER:
        out[st.value] = await db.cases.count_documents({**base, "stage": st.value})
    return out


async def get_case(db, user: CurrentUser, case_id: str,
                   lang: str = DEFAULT_LANGUAGE) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This case is not yours")
    if user.role == Role.PARTNER:
        # Allow either a bulk client assignment or an individual delegated task.
        has_task = await db.tasks.find_one(
            {"case_id": case_id, "assignee_id": user.id}, {"_id": 1})
        if not has_task:
            await assert_case_access(db, user, doc)
    out = await attach_consultant(db, serialize(doc))
    out["stage_label"] = _stage_label(doc, doc.get("stage"), lang)

    # Matched on the case OR on the request it came from. A document is stamped
    # with `case_id` when the case is opened, but a case opened before that
    # binding existed - or one whose request gained documents afterwards - has
    # documents that only carry `request_id`. Reading both is what stops the
    # case showing "No documents linked yet" while the request lists two.
    doc_query: Dict[str, Any] = {"case_id": case_id}
    if doc.get("request_id"):
        doc_query = {"$or": [{"case_id": case_id},
                             {"request_id": doc["request_id"]}]}
    out["documents"] = [
        serialize(d) async for d in db.documents.find(doc_query).sort("created_at", 1)
    ]
    # Stamp the ones that were only reachable through the request, so every
    # other case-scoped query - a delegated partner's document list among them
    # - finds them too. Cheap, and it happens once per document.
    unstamped = [d["id"] for d in out["documents"] if not d.get("case_id")]
    if unstamped:
        await db.documents.update_many(
            {"_id": {"$in": [oid(i) for i in unstamped]}},
            {"$set": {"case_id": case_id, "updated_at": utcnow()}},
        )
        for d in out["documents"]:
            d.setdefault("case_id", case_id)
            d["case_id"] = d["case_id"] or case_id
    for doc_item in out["documents"]:
        if "status" in doc_item:
            doc_item["status_label"] = translate(f"document_status.{doc_item['status']}", lang)
    out["client_tasks"] = [
        serialize(t) async for t in db.tasks.find(
            {"case_id": case_id, "assignee_type": TaskAssigneeType.CLIENT.value}
        ).sort("created_at", 1)
    ]
    out["partner_tasks"] = [
        serialize(t) async for t in db.tasks.find(
            {"case_id": case_id, "assignee_type": TaskAssigneeType.PARTNER.value}
        ).sort("created_at", 1)
    ]
    for task_item in out["client_tasks"] + out["partner_tasks"]:
        if "status" in task_item:
            task_item["status_label"] = translate(f"task_status.{task_item['status']}", lang)
    # The case's own stage order (a procedure's stages when it has one, the
    # fixed ladder otherwise) - see `_stage_order`. `CaseDetail.tsx` builds its
    # stage picker and progress bar from this, so a procedure-driven case
    # sending it the wrong list would put "Advance Stage" back to offering
    # the fixed immigration stages this fix was meant to stop happening.
    out["stage_order"] = _stage_order(doc)
    out["stage_labels"] = {key: _stage_label(doc, key, lang) for key in out["stage_order"]}
    apply_progress(
        out,
        total=len(out["documents"]),
        approved=sum(1 for d in out["documents"] if d.get("status") == APPROVED),
    )
    return out


async def update_case(db, user: CurrentUser, case_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    await assert_case_access(db, user, doc)
    payload = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
    payload["updated_at"] = utcnow()
    await db.cases.update_one({"_id": oid(case_id)}, {"$set": payload})
    return await attach_consultant(db, serialize(await _get(db, case_id)))


async def advance_stage(db, user: CurrentUser, case_id: str, data) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    await assert_case_access(db, user, doc)
    order = _stage_order(doc)
    current = doc["stage"]

    if data.stage:
        target = data.stage
        if target not in order:
            raise BadRequest(f"'{target}' is not a stage of this case")
    else:
        idx = order.index(current) if current in order else -1
        if idx >= len(order) - 1:
            raise BadRequest("This case is already completed")
        target = order[idx + 1]

    # C6: a case used to close with documents still missing, at whatever
    # progress the checklist happened to be at, with no warning. Moving into
    # the last stage of the workflow - "Completed", or a procedure's own
    # final stage such as "Decision" - now has to be a decision the
    # consultant actually makes, not something that happens silently.
    is_closing = order and target == order[-1]
    if is_closing and not getattr(data, "force", False):
        from app.core.enums import DocumentStatus

        outstanding = await db.documents.count_documents({
            "case_id": case_id, "is_required": True,
            "status": {"$ne": DocumentStatus.APPROVED.value},
        })
        if outstanding:
            raise BadRequest(
                f"{outstanding} required document(s) are not approved yet. "
                f"Confirm to close the case anyway, or approve them first."
            )

    now = utcnow()
    owner_id = data.owner_id or user.id
    await db.cases.update_one(
        {"_id": oid(case_id)},
        {"$set": {"stage": target, "progress": _progress_for(doc, target), "updated_at": now,
                  "stage_owner_id": owner_id, "stage_deadline": data.deadline},
         "$push": {"timeline": {"stage": target, "at": now, "by": user.id,
                                "note": data.note, "owner_id": owner_id,
                                "deadline": data.deadline}}},
    )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.CASE_STAGE_CHANGED,
                 title_key="notify.case_stage_changed",
                 params={"reference": doc["reference"],
                         "stage": _stage_label(doc, target).lower()},
                 body=data.note or "", data={"case_id": case_id})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action=f"{_ADVANCED_PREFIX}{target}", subject=doc["reference"],
                       case_id=case_id)

    # Whichever stage this case is leaving no longer needs chasing on the
    # calendar; whichever one it just entered does, if a deadline was given
    # for it. The fixed ladder keeps its two named kinds (so the Agenda screen
    # can still show "Filing" / "Authority response" specifically); a
    # procedure's own stages get a generic one, since "documents" / "review"
    # / "decision" carry no such fixed meaning.
    await deadlines.clear(db, kind="filing", source_collection="cases", source_id=case_id)
    await deadlines.clear(db, kind="authority_response", source_collection="cases",
                          source_id=case_id)
    await deadlines.clear(db, kind="stage_deadline", source_collection="cases", source_id=case_id)
    is_fixed_ladder = not doc.get("workflow_stages")
    stage_deadline_kind = ({
        CaseStage.GOVERNMENT_SUBMISSION.value: "filing",
        CaseStage.GOVERNMENT_PROCESSING.value: "authority_response",
    }.get(target) if is_fixed_ladder else None) or "stage_deadline"
    if data.deadline:
        await deadlines.upsert(
            db, kind=stage_deadline_kind, source_collection="cases", source_id=case_id,
            due_date=data.deadline, title=f"{doc['reference']} - {_stage_label(doc, target)}",
            consultant_id=doc.get("consultant_id"), client_id=doc["client_id"],
            client_name=doc.get("client_name"), case_id=case_id,
            case_reference=doc["reference"], owner_id=owner_id,
        )
    return await attach_consultant(db, serialize(await _get(db, case_id)))


async def authority_request(db, user: CurrentUser, case_id: str, data) -> Dict[str, Any]:
    """One round of the cycle that can repeat before a case reaches a
    decision: the authority asks for more, the consultant answers it, and
    the same case may go round again. Each call is its own entry - a case
    that heard from the authority three times has three of these, not one
    overwritten note - and each carries its own response deadline.
    """
    doc = await _get(db, case_id)
    await assert_case_access(db, user, doc)
    now = utcnow()
    entry = {"note": data.note, "response_due": data.response_due,
             "requested_by": user.id, "requested_by_name": user.raw.get("full_name", ""),
             "at": now, "resolved": False}
    await db.cases.update_one({"_id": oid(case_id)}, {"$push": {"authority_requests": entry},
                                                       "$set": {"updated_at": now}})
    await log_activity(db, actor_id=user.id, actor_name=user.raw.get("full_name", ""),
                       action="logged an authority request for", subject=doc["reference"],
                       case_id=case_id)
    if data.response_due:
        await deadlines.upsert(
            db, kind="authority_response", source_collection="cases", source_id=case_id,
            due_date=data.response_due,
            title=f"{doc['reference']} - {translate('deadline.authority_response', DEFAULT_LANGUAGE)}",
            consultant_id=doc.get("consultant_id"), client_id=doc["client_id"],
            client_name=doc.get("client_name"), case_id=case_id,
            case_reference=doc["reference"], owner_id=doc.get("consultant_id"),
        )
    await notify(db, user_ids=[doc["client_id"]], type=NotificationType.CASE_STAGE_CHANGED,
                 title_key="notify.authority_request_logged",
                 params={"reference": doc["reference"]}, body=data.note,
                 data={"case_id": case_id})
    return await attach_consultant(db, serialize(await _get(db, case_id)))


async def ai_fill_form(db, user: CurrentUser, case_id: str) -> Dict[str, Any]:
    """Item 7.3: read the case's own documents and pre-fill its intake form,
    for the consultant to check rather than type from scratch.

    Never overwrites a field the consultant already corrected by hand - only
    ones still empty, or still carrying an earlier AI guess - so re-running
    this after a new document arrives fills in the gaps without undoing a
    consultant's own edit.
    """
    doc = await _get(db, case_id)
    await assert_case_access(db, user, doc)
    fields = doc.get("client_fields") or []
    if not fields:
        return {"field_values": doc.get("field_values") or {}}

    documents = [
        {"name": d.get("name"), "category": d.get("category"),
         "summary": (d.get("ai_analysis") or {}).get("summary"),
         "extracted_fields": (d.get("ai_analysis") or {}).get("extracted_fields")}
        async for d in db.documents.find({"case_id": case_id})
        if d.get("ai_analysis")
    ]
    filled = await fill_case_form(
        fields=[{"key": f.get("key"), "label": f.get("label"), "type": f.get("type")}
                for f in fields],
        documents=documents,
    )
    existing = dict(doc.get("field_values") or {})
    now = utcnow()
    for key, result in filled.items():
        current = existing.get(key)
        if current and current.get("source") in ("consultant", "client"):
            continue  # a person's own answer is never overwritten by a guess
        existing[key] = {"value": result.get("value"), "source": "ai",
                         "confidence": result.get("confidence", 0),
                         "source_document": result.get("source_document"),
                         "updated_at": now}
    await db.cases.update_one({"_id": oid(case_id)},
                              {"$set": {"field_values": existing, "updated_at": now}})
    return {"field_values": existing}


async def update_form_field(db, user: CurrentUser, case_id: str, field_key: str,
                            data) -> Dict[str, Any]:
    """A correction to one field of the case's intake form - always wins over
    an AI guess, and is never silently replaced by a later `ai_fill_form` run.

    C2's other half: the client had nowhere to give the platform their own
    personal data, so this is open to them for their own case, not just the
    consultant. Either source is trusted the same way once written - the
    person answering "what is my passport number" is whoever actually knows
    it, and that is at least as often the client as the consultant.
    """
    doc = await _get(db, case_id)
    if user.role == Role.CLIENT:
        if doc["client_id"] != user.id:
            raise Forbidden("This case is not yours")
        source = "client"
    else:
        await assert_case_access(db, user, doc)
        source = "consultant"
    existing = dict(doc.get("field_values") or {})
    existing[field_key] = {"value": data.value, "source": source,
                           "confidence": 100, "updated_at": utcnow()}
    await db.cases.update_one({"_id": oid(case_id)},
                              {"$set": {"field_values": existing, "updated_at": utcnow()}})
    return {"field_values": existing}


async def case_history(db, user: CurrentUser, case_id: str,
                       lang: str = DEFAULT_LANGUAGE) -> List[Dict[str, Any]]:
    """Every dated, attributed thing that has happened on a case, in order -
    uploads, approvals, rejections, document requests withdrawn or answered,
    partner tasks assigned, stage advances, authority requests - answering
    item 7.1 directly: a case history a consultant can actually read, rather
    than the same facts scattered one per screen with no single place
    listing them.

    Reads `activities`, which every one of those actions already writes to
    via `log_activity` - this is the first thing that reads it back scoped to
    one case, not a second log kept in parallel.
    """
    doc = await _get(db, case_id)
    if user.role == Role.CLIENT and doc["client_id"] != user.id:
        raise Forbidden("This case is not yours")
    if user.role == Role.PARTNER:
        has_task = await db.tasks.find_one(
            {"case_id": case_id, "assignee_id": user.id}, {"_id": 1})
        if not has_task:
            await assert_case_access(db, user, doc)

    events = [serialize(a) async for a in
             db.activities.find({"case_id": case_id}).sort("created_at", 1)]
    for event in events:
        action = event.get("action") or ""
        if action.startswith(_ADVANCED_PREFIX):
            stage_code = action[len(_ADVANCED_PREFIX):]
            event["action_label"] = translate(
                "activity.advanced_to_stage", lang,
                stage=translate(f"stage.{stage_code}", lang))
        else:
            key = _ACTIVITY_LABEL_KEYS.get(action)
            event["action_label"] = translate(key, lang) if key else action
    return events


async def generate_guidance(db, user: CurrentUser, case_id: str, create_tasks: bool = True) -> Dict[str, Any]:
    doc = await _get(db, case_id)
    await assert_case_access(db, user, doc)
    documents = [
        {"name": d["name"], "status": d["status"], "category": d.get("category")}
        async for d in db.documents.find({"case_id": case_id})
    ]
    guidance = await case_guidance(case_context={
        "reference": doc["reference"],
        "case_type": doc["case_type"],
        "destination_country": doc.get("destination_country"),
        "stage": doc["stage"],
        "deadline": doc.get("deadline"),
        "documents": documents,
    })
    await db.cases.update_one(
        {"_id": oid(case_id)},
        {"$set": {"ai_guidance": guidance, "guidance_generated_at": utcnow(),
                  "updated_at": utcnow()}},
    )

    if create_tasks and guidance.get("client_tasks"):
        from datetime import timedelta
        now = utcnow()
        rows = []
        for t in guidance["client_tasks"]:
            rows.append({
                "title": t.get("title", "Client task"),
                "description": t.get("description"),
                "case_id": case_id,
                "case_reference": doc["reference"],
                "client_id": doc["client_id"],
                "consultant_id": doc.get("consultant_id"),
                "assignee_type": TaskAssigneeType.CLIENT.value,
                "assignee_id": doc["client_id"],
                "assignee_name": doc.get("client_name"),
                "status": TaskStatus.PENDING.value,
                "auto_created": True,
                "due_date": now + timedelta(days=int(t.get("due_in_days", 7) or 7)),
                "created_at": now,
                "updated_at": now,
            })
        if rows:
            await db.tasks.insert_many(rows)
            await notify(db, user_ids=[doc["client_id"]],
                         type=NotificationType.TASK_ASSIGNED,
                         title_key="notify.case_tasks_added",
                         params={"count": len(rows),
                                 "reference": doc["reference"]},
                         data={"case_id": case_id})
    return guidance


async def timeline(db, case_id: str,
                   lang: str = DEFAULT_LANGUAGE) -> List[Dict[str, Any]]:
    doc = await _get(db, case_id)
    done = {entry["stage"]: entry for entry in doc.get("timeline", [])}
    return [
        {"stage": key,
         "label": _stage_label(doc, key, lang),
         "completed": key in done,
         "at": done.get(key, {}).get("at"),
         "current": doc["stage"] == key}
        for key in _stage_order(doc)
    ]
