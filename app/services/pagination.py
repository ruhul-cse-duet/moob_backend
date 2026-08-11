from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.utils import serialize
from app.schemas.common import PageParams


async def paginate(
    db: AsyncIOMotorDatabase,
    collection: str,
    query: Dict[str, Any],
    params: PageParams,
    sort: Optional[List[Tuple[str, int]]] = None,
    enrich: bool = True,
) -> Dict[str, Any]:
    """
    Standard page envelope.

    When any row carries `consultant_id` (or `consultant_ids`), the owning consultant
    is resolved in ONE extra query for the whole page and attached as `consultant`.
    That keeps "which consultant owns this" answerable from any list response without
    the client making N follow-up calls.

    Enrichment is skipped automatically on the platform database, which has no
    tenant `users` collection — detected by name so no call site has to remember a flag.
    """
    coll = db[collection]
    total = await coll.count_documents(query)
    cursor = coll.find(query)
    if sort:
        cursor = cursor.sort(sort)
    cursor = cursor.skip(params.skip).limit(params.page_size)
    items = [serialize(doc) async for doc in cursor]

    is_tenant_db = db.name != settings.PLATFORM_DB_NAME
    if (enrich and is_tenant_db
            and any(i.get("consultant_id") or i.get("consultant_ids") for i in items)):
        from app.services.ownership import attach_consultants
        items = await attach_consultants(db, items)

    pages = (total + params.page_size - 1) // params.page_size
    return {
        "success": True,
        "message": "OK",
        "items": items,
        "total": total,
        "page": params.page,
        "page_size": params.page_size,
        "pages": pages,
    }
