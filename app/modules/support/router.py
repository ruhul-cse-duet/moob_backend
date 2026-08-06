"""support/HelpSupport.tsx — the customer-facing side of the helpdesk. [INFERRED]"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, status as http

from app.core.deps import CurrentUser, get_current_user, page_params
from app.core.enums import TicketStatus
from app.modules.support import schemas as s
from app.modules.support import service
from app.schemas.common import PageParams

router = APIRouter(prefix="/support", tags=["Help & Support"])


@router.post("/tickets", status_code=http.HTTP_201_CREATED, summary="Raise a support ticket")
async def create_ticket(payload: s.TicketCreate,
                        user: CurrentUser = Depends(get_current_user)):
    return await service.create_ticket(user, payload)


@router.get("/tickets", summary="My tickets")
async def my_tickets(status: Optional[TicketStatus] = Query(None),
                     params: PageParams = Depends(page_params),
                     user: CurrentUser = Depends(get_current_user)):
    return await service.list_tickets(params, user=user, status=status)


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str, user: CurrentUser = Depends(get_current_user)):
    return await service.get_ticket(ticket_id, user=user)


@router.post("/tickets/{ticket_id}/reply")
async def reply(ticket_id: str, payload: s.TicketReply,
                user: CurrentUser = Depends(get_current_user)):
    return await service.reply(ticket_id, payload.body, user=user)
