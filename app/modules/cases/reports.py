"""AnalysisReport.tsx — the consolidated AI view for one case or request. [INFERRED]"""
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    require_active_tenant,
)
from app.core.enums import DocumentStatus, Role
from app.core.exceptions import Forbidden, NotFound
from app.core.utils import oid, serialize, utcnow

router = APIRouter(prefix="/analysis", tags=["AI Analysis Report"],
                   dependencies=[Depends(require_active_tenant)])


@router.get("/case/{case_id}", summary="Consolidated AI analysis across a case's documents")
async def case_analysis_report(case_id: str,
                               user: CurrentUser = Depends(get_current_user),
                               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    case = await db.cases.find_one({"_id": oid(case_id)})
    if not case:
        raise NotFound("Case not found")
    if user.role == Role.CLIENT and case["client_id"] != user.id:
        raise Forbidden("This case is not yours")

    documents = [serialize(d) async for d in db.documents.find({"case_id": case_id})]
    return _build(case, documents, user)


@router.get("/request/{request_id}", summary="Consolidated AI analysis for one request")
async def request_analysis_report(request_id: str,
                                  user: CurrentUser = Depends(get_current_user),
                                  db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    req = await db.requests.find_one({"_id": oid(request_id)})
    if not req:
        raise NotFound("Request not found")
    if user.role == Role.CLIENT and req["client_id"] != user.id:
        raise Forbidden("This request is not yours")
    documents = [serialize(d) async for d in db.documents.find({"request_id": request_id})]
    return _build(req, documents, user)


def _build(parent: Dict[str, Any], documents: List[Dict[str, Any]],
           user: CurrentUser) -> Dict[str, Any]:
    analysed = [d for d in documents if d.get("ai_analysis")]
    confidences = [d["ai_analysis"].get("confidence", 0) or 0 for d in analysed]

    issues: List[Dict[str, Any]] = []
    for d in analysed:
        for issue in d["ai_analysis"].get("issues", []) or []:
            issues.append({"document": d["name"], "issue": issue,
                           "document_id": d["id"], "status": d["status"]})

    flagged = [d for d in analysed
               if (d["ai_analysis"].get("recommendation") in {"request_reupload",
                                                              "manual_review"}
                   or (d["ai_analysis"].get("confidence") or 0) < 80)]

    report = {
        "reference": parent.get("reference"),
        "consultant_id": parent.get("consultant_id"),
        "generated_at": utcnow(),
        "documents_total": len(documents),
        "documents_analysed": len(analysed),
        "documents_approved": len([d for d in documents
                                   if d["status"] == DocumentStatus.APPROVED.value]),
        "average_confidence": round(sum(confidences) / len(confidences), 1)
        if confidences else None,
        "lowest_confidence": min(confidences) if confidences else None,
        "issues": issues,
        "flagged_for_review": [{"document_id": d["id"], "name": d["name"],
                                "confidence": d["ai_analysis"].get("confidence"),
                                "recommendation": d["ai_analysis"].get("recommendation"),
                                "summary": d["ai_analysis"].get("summary")}
                               for d in flagged],
        "extracted_fields": {d["name"]: d["ai_analysis"].get("extracted_fields", {})
                             for d in analysed},
        "expiring_documents": [{"document_id": d["id"], "name": d["name"],
                                "expiry_date": d["ai_analysis"].get("expiry_date")}
                               for d in analysed if d["ai_analysis"].get("expiry_date")],
    }
    if parent.get("ai_guidance"):
        report["case_guidance"] = parent["ai_guidance"]
    if user.role == Role.CLIENT:
        # Clients see their own document status, not the consultant's internal triage.
        report.pop("flagged_for_review", None)
    return report
