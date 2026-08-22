from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
)
from app.core.enums import Role
from app.core.exceptions import NotFound
from app.core.utils import oid, serialize, utcnow
from app.schemas.common import Message, PageParams
from app.services.openai_service import assistant_reply
from app.services.pagination import paginate

router = APIRouter(prefix="/ai", tags=["AI Assistant"],
                   dependencies=[Depends(require_active_tenant)])


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    case_id: Optional[str] = None
    client_id: Optional[str] = None


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    consultant_id: Optional[str] = None
    client_id: Optional[str] = None
    partner_id: Optional[str] = None
    messages: List[dict] = []


def _own(user: CurrentUser) -> Dict[str, Any]:
    """Every role has an assistant, and nobody sees anyone else's threads."""
    return {"user_id": user.id}


async def _case_context(db, user: CurrentUser, case_id: str) -> Dict[str, Any]:
    """The case, but only if this account is actually on it."""
    case = await db.cases.find_one({"_id": oid(case_id)})
    if not case:
        return {}
    allowed = {case.get("client_id"), case.get("consultant_id"), case.get("partner_id")}
    if not user.is_consultant and user.id not in allowed:
        return {}
    return {
        "case_reference": case["reference"],
        "case_type": case["case_type"],
        "case_stage": case["stage"],
        "destination_country": case.get("destination_country"),
    }


@router.post("/chat", response_model=ChatResponse,
             summary="Ask the AI assistant (answers are shaped to the caller's role)")
async def chat(payload: ChatRequest,
               user: CurrentUser = Depends(get_current_user),
               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    consultant_id = user.consultant_id
    convo = None

    if payload.conversation_id:
        convo = await db.ai_conversations.find_one(
            {"_id": oid(payload.conversation_id), **_own(user)}
        )
        if not convo:
            raise NotFound("Conversation not found")
        history = convo.get("messages", [])
        convo_id = payload.conversation_id
    else:
        result = await db.ai_conversations.insert_one({
            "user_id": user.id,
            "role": user.role.value if isinstance(user.role, Role) else user.role,
            "consultant_id": consultant_id,
            # A consultant may be asking on behalf of a client; everyone else is
            # only ever asking about themselves.
            "client_id": payload.client_id if user.is_consultant else user.id,
            "partner_id": user.id if user.role == Role.PARTNER else user.raw.get("partner_id"),
            "case_id": payload.case_id,
            "title": payload.message[:60],
            "messages": [],
            "created_at": now,
            "updated_at": now,
        })
        history, convo_id = [], str(result.inserted_id)

    context: Dict[str, Any] = {
        "user_name": user.raw.get("full_name", "there"),
        "role": user.role.value if isinstance(user.role, Role) else user.role,
        "tenant_context": "isolated workspace for this consultancy",
    }
    if payload.case_id:
        context.update(await _case_context(db, user, payload.case_id))

    reply = await assistant_reply(
        history=[{"role": m["role"], "content": m["content"]} for m in history],
        message=payload.message,
        context=context,
    )

    user_msg = {"role": "user", "content": payload.message,
                "sender_name": user.raw.get("full_name", ""), "at": now}
    assistant_msg = {"role": "assistant", "content": reply,
                     "sender_name": "AI Assistant", "at": utcnow()}

    await db.ai_conversations.update_one(
        {"_id": oid(convo_id)},
        {"$push": {"messages": {"$each": [user_msg, assistant_msg]}},
         "$set": {"updated_at": utcnow()}},
    )

    updated = await db.ai_conversations.find_one({"_id": oid(convo_id)})
    return {
        "conversation_id": convo_id,
        "reply": reply,
        "consultant_id": consultant_id,
        "client_id": updated.get("client_id"),
        "partner_id": updated.get("partner_id"),
        "messages": [serialize(m) for m in updated.get("messages", [])],
    }


@router.get("/conversations", summary="Your assistant history")
async def conversations(params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "ai_conversations", _own(user), params,
                          sort=[("updated_at", -1)])


@router.get("/conversations/{conversation_id}", summary="Get conversation messages")
async def conversation(conversation_id: str,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.ai_conversations.find_one({"_id": oid(conversation_id), **_own(user)})
    if not doc:
        raise NotFound("Conversation not found")
    return serialize(doc)


@router.delete("/conversations/{conversation_id}", response_model=Message,
               summary="Delete AI conversation")
async def delete_conversation(conversation_id: str,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    result = await db.ai_conversations.delete_one({"_id": oid(conversation_id), **_own(user)})
    if not result.deleted_count:
        raise NotFound("Conversation not found")
    return {"detail": "Conversation deleted"}
