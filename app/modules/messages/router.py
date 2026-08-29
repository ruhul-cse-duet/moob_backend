"""Conversations between the people working on a case.

The rules about who may message whom live in `service.contact_ids`; every route
here goes through it rather than trusting the ids it was handed.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
)
from app.modules.messages import service
from app.schemas.common import PageParams
from app.services import storage

router = APIRouter(prefix="/messages", tags=["Messages"],
                   dependencies=[Depends(require_active_tenant)])


class ThreadCreate(BaseModel):
    participant_ids: List[str]
    case_id: Optional[str] = None
    subject: Optional[str] = None


class SendMessage(BaseModel):
    body: str = Field(default="", max_length=4000)
    # The sending client's own Socket.IO id. Optional, and only ever used to
    # leave that one socket out of the broadcast - the sender already has the
    # message in this call's response, so an echo is a second copy of it.
    socket_id: Optional[str] = Field(default=None, max_length=64)


@router.get("/contacts", summary="Who I am allowed to message")
async def contacts(user: CurrentUser = Depends(get_current_user),
                   db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """A client sees their consultant; a consultant sees their clients and
    partners. Existing conversations come back attached, so the app can open
    one without a second call."""
    return await service.contacts(db, user)


@router.get("/unread", summary="Unread messages across every conversation")
async def unread(user: CurrentUser = Depends(get_current_user),
                 db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return {"unread": await service.unread_total(db, user)}


@router.post("/threads", status_code=201, summary="Open, or reuse, a conversation")
async def create_thread(payload: ThreadCreate,
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.open_thread(db, user, payload.participant_ids,
                                     payload.case_id, payload.subject)


@router.get("/threads", summary="My conversations, most recent first")
async def list_threads(params: PageParams = Depends(page_params),
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.list_threads(db, user, params)


@router.get("/threads/{thread_id}", summary="The messages in one conversation")
async def thread_messages(thread_id: str,
                          params: PageParams = Depends(page_params),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.thread_messages(db, user, thread_id, params)


@router.post("/threads/{thread_id}", status_code=201, summary="Send a message")
async def send_message(thread_id: str,
                       payload: SendMessage,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """The response *is* the sent message - the app should render this one.

    Pass `socket_id` (the value the Socket.IO client reports as its own id) and
    that socket is left out of the broadcast, so the sender's other devices
    still get the message live while the sending device does not receive a
    second copy of what it already has here.
    """
    return await service.send(db, user, thread_id, payload.body,
                              socket_id=payload.socket_id)


@router.post("/threads/{thread_id}/attachment", status_code=201,
             summary="Send a photo or a document")
async def send_attachment(thread_id: str,
                          file: UploadFile = File(...),
                          body: str = Form(""),
                          socket_id: Optional[str] = Form(None),
                          user: CurrentUser = Depends(get_current_user),
                          db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    """One round trip: the file is stored and posted as a message together, so
    an upload that succeeds can never leave a message that never arrives."""
    return await service.send_attachment(db, user, thread_id, file, body,
                                         socket_id=socket_id)


@router.get("/attachments/{message_id}", summary="Download what was sent")
async def download_attachment(message_id: str,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    attachment = await service.attachment_for(db, user, message_id)
    return StreamingResponse(
        storage.stream_file(db, attachment["file_id"], service.ATTACHMENTS_BUCKET),
        media_type=attachment.get("mime") or "application/octet-stream",
        headers={
            # inline: a photo shared in a chat should open, not land in Downloads.
            "Content-Disposition":
                f'inline; filename="{storage.safe_filename(attachment.get("name"))}"'
        },
    )


@router.post("/threads/{thread_id}/read", summary="Mark a conversation as read")
async def mark_read(thread_id: str,
                    user: CurrentUser = Depends(get_current_user),
                    db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await service.mark_read(db, user, thread_id)
