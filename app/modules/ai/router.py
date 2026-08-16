from typing import List, Optional

from fastapi import APIRouter, Depends
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_tenant_db,
    page_params,
    require_active_tenant,
    require_consultant,
)
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
    consultant_id: str
    client_id: Optional[str] = None
    partner_id: Optional[str] = None
    messages: List[dict] = []


@router.post("/chat", response_model=ChatResponse, summary="Ask the AI assistant (Consultant only)")
async def chat(payload: ChatRequest,
               user: CurrentUser = Depends(require_consultant),
               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    consultant_id = user.consultant_id or user.id
    if payload.conversation_id:
        convo = await db.ai_conversations.find_one({"_id": oid(payload.conversation_id), "consultant_id": consultant_id})
        history = convo.get("messages", []) if convo else []
        convo_id = payload.conversation_id
    else:
        result = await db.ai_conversations.insert_one({
            "user_id": user.id,
            "consultant_id": consultant_id,
            "client_id": payload.client_id,
            "partner_id": user.raw.get("partner_id"),
            "case_id": payload.case_id,
            "title": payload.message[:60],
            "messages": [],
            "created_at": now,
            "updated_at": now,
        })
        history, convo_id = [], str(result.inserted_id)

    # Inject consultant and multi-tenant context for the AI
    context = {
        "consultant_name": user.raw.get("full_name", "Consultant"),
        "consultant_id": consultant_id,
        "role": user.role,
        "tenant_context": "isolated workspace for this consultant's company",
    }
    
    if payload.case_id:
        case = await db.cases.find_one({"_id": oid(payload.case_id)})
        if case:
            context.update({
                "case_reference": case["reference"],
                "case_type": case["case_type"],
                "case_stage": case["stage"],
                "destination_country": case.get("destination_country")
            })

    reply = await assistant_reply(
        history=[{"role": m["role"], "content": m["content"]} for m in history],
        message=payload.message,
        context=context,
    )
    
    user_msg = {"role": "user", "content": payload.message, "sender_name": user.raw.get("full_name", "Consultant"), "at": now}
    assistant_msg = {"role": "assistant", "content": reply, "sender_name": "AI Assistant", "at": utcnow()}

    await db.ai_conversations.update_one(
        {"_id": oid(convo_id)},
        {"$push": {"messages": {"$each": [user_msg, assistant_msg]}},
         "$set": {"updated_at": utcnow()}},
    )
    
    updated_convo = await db.ai_conversations.find_one({"_id": oid(convo_id)})
    
    return {
        "conversation_id": convo_id,
        "reply": reply,
        "consultant_id": consultant_id,
        "client_id": payload.client_id or updated_convo.get("client_id"),
        "partner_id": user.raw.get("partner_id"),
        "messages": serialize(updated_convo.get("messages", [])),
    }


@router.get("/conversations", summary="Assistant history (Consultant only)")
async def conversations(params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(require_consultant),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "ai_conversations", {"consultant_id": user.consultant_id or user.id}, params,
                          sort=[("updated_at", -1)])


@router.get("/conversations/{conversation_id}", summary="Get conversation messages (Consultant only)")
async def conversation(conversation_id: str,
                       user: CurrentUser = Depends(require_consultant),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.ai_conversations.find_one({"_id": oid(conversation_id), "consultant_id": user.consultant_id or user.id})
    return serialize(doc)


@router.delete("/conversations/{conversation_id}", response_model=Message, summary="Delete AI conversation")
async def delete_conversation(conversation_id: str,
                              user: CurrentUser = Depends(require_consultant),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.ai_conversations.delete_one({"_id": oid(conversation_id), "consultant_id": user.consultant_id or user.id})
    return {"detail": "Conversation deleted"}
