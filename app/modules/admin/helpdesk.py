"""admin/Helpdesk.tsx — one ticket queue across every tenant.  [INFERRED]"""
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query

from app.core.deps import CurrentUser, page_params, require_super_admin
from app.core.enums import TicketPriority, TicketStatus
from app.core.utils import oid
from app.db.mongo import platform_db
from app.modules.support import schemas as s
from app.modules.support import service
from app.schemas.common import PageParams

router = APIRouter(prefix="/helpdesk", tags=["Super Admin · Helpdesk"])


@router.get("/tickets", summary="Platform-wide ticket queue")
async def queue(status: Optional[TicketStatus] = Query(None),
                priority: Optional[TicketPriority] = Query(None),
                tenant_id: Optional[str] = Query(None),
                assigned_to: Optional[str] = Query(None),
                search: Optional[str] = Query(None),
                params: PageParams = Depends(page_params),
                user: CurrentUser = Depends(require_super_admin)):
    page = await service.list_tickets(params, admin=True, status=status, priority=priority,
                                      tenant_id=tenant_id, assigned_to=assigned_to,
                                      search=search)
    names = {}
    for item in page["items"]:
        tid = item.get("tenant_id")
        if tid and tid not in names:
            t = await platform_db().tenants.find_one({"_id": oid(tid)})
            names[tid] = t["name"] if t else None
        item["organization_name"] = names.get(tid)
    return page


@router.get("/stats", summary="Queue counters for the helpdesk header")
async def stats(user: CurrentUser = Depends(require_super_admin)):
    db = platform_db()
    counts = {st.value: await db.support_tickets.count_documents({"status": st.value})
              for st in TicketStatus}
    counts["unassigned"] = await db.support_tickets.count_documents(
        {"assigned_to": None, "status": {"$nin": ["resolved", "closed"]}})
    counts["urgent_open"] = await db.support_tickets.count_documents(
        {"priority": TicketPriority.URGENT.value,
         "status": {"$nin": ["resolved", "closed"]}})
    return counts


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str, user: CurrentUser = Depends(require_super_admin)):
    return await service.get_ticket(ticket_id, admin=True)


@router.post("/tickets/{ticket_id}/reply")
async def reply(ticket_id: str, payload: s.TicketReply,
                user: CurrentUser = Depends(require_super_admin)):
    return await service.reply(ticket_id, payload.body, user=user, admin=True)


@router.post("/tickets/{ticket_id}/status")
async def set_status(ticket_id: str, status: TicketStatus = Body(embed=True),
                     user: CurrentUser = Depends(require_super_admin)):
    return await service.set_status(ticket_id, status, user)


@router.post("/tickets/{ticket_id}/assign")
async def assign(ticket_id: str, agent_id: Optional[str] = Body(None, embed=True),
                 user: CurrentUser = Depends(require_super_admin)):
    return await service.assign(ticket_id, agent_id)
