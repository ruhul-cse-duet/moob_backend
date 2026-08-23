"""How far along a case is, measured by its documents.

A case used to report progress from its stage: position in CASE_STAGE_ORDER,
turned into a percentage. That number moved only when a consultant advanced the
stage, so it said nothing about the work actually outstanding - a case could sit
at 30% with every document approved, or at 100% with nothing collected.

Progress is now what the client can see for themselves: approved documents over
all documents on the case. All approved is 100%. The requests module already
reported progress this way; cases were the odd one out.

A case with no documents yet has nothing to measure, so it keeps the stage
number - otherwise a case that has not reached the document stage would read 0%
and a completed one that never needed documents would too.
"""
from typing import Any, Dict, Iterable, List, Optional

from app.core.enums import DocumentStatus

APPROVED = DocumentStatus.APPROVED.value


def progress_from_counts(total: int, approved: int) -> Optional[int]:
    """Percentage approved, or None when there is nothing to measure."""
    if total <= 0:
        return None
    return round(approved / total * 100)


def progress_from_documents(documents: Iterable[Dict[str, Any]]) -> Optional[int]:
    documents = list(documents)
    approved = sum(1 for d in documents if d.get("status") == APPROVED)
    return progress_from_counts(len(documents), approved)


def apply_progress(case: Dict[str, Any], total: int, approved: int) -> Dict[str, Any]:
    """Overlay the document counts and the progress they imply, in place.

    The stored `progress` is left alone when the case has no documents, so the
    stage-derived value stays the fallback rather than being overwritten with a
    zero that means "unmeasurable" rather than "nothing done".
    """
    case["documents_total"] = total
    case["documents_approved"] = approved
    derived = progress_from_counts(total, approved)
    if derived is not None:
        case["progress"] = derived
    return case


async def attach_case_progress(db, cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Same, for a whole page of cases, in one query rather than two per case."""
    ids = [c["id"] for c in cases if c.get("id")]
    if not ids:
        return cases

    tally: Dict[str, Dict[str, int]] = {}
    async for row in db.documents.aggregate([
        {"$match": {"case_id": {"$in": ids}}},
        {"$group": {
            "_id": "$case_id",
            "total": {"$sum": 1},
            "approved": {"$sum": {"$cond": [{"$eq": ["$status", APPROVED]}, 1, 0]}},
        }},
    ]):
        tally[row["_id"]] = {"total": row["total"], "approved": row["approved"]}

    for case in cases:
        counts = tally.get(case.get("id"), {"total": 0, "approved": 0})
        apply_progress(case, counts["total"], counts["approved"])
    return cases
