from typing import Optional

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


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str


@router.post("/chat", response_model=ChatResponse, summary="Ask the AI assistant")
async def chat(payload: ChatRequest,
               user: CurrentUser = Depends(get_current_user),
               db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    now = utcnow()
    if payload.conversation_id:
        convo = await db.ai_conversations.find_one({"_id": oid(payload.conversation_id)})
        history = convo.get("messages", []) if convo else []
        convo_id = payload.conversation_id
    else:
        result = await db.ai_conversations.insert_one({
            "user_id": user.id, "case_id": payload.case_id,
            "title": payload.message[:60], "messages": [],
            "created_at": now, "updated_at": now,
        })
        history, convo_id = [], str(result.inserted_id)

    context = None
    if payload.case_id:
        case = await db.cases.find_one({"_id": oid(payload.case_id)})
        if case:
            context = {"reference": case["reference"], "case_type": case["case_type"],
                       "stage": case["stage"],
                       "destination_country": case.get("destination_country")}

    reply = await assistant_reply(
        history=[{"role": m["role"], "content": m["content"]} for m in history],
        message=payload.message,
        context=context,
    )
    await db.ai_conversations.update_one(
        {"_id": oid(convo_id)},
        {"$push": {"messages": {"$each": [
            {"role": "user", "content": payload.message, "at": now},
            {"role": "assistant", "content": reply, "at": utcnow()},
        ]}}, "$set": {"updated_at": utcnow()}},
    )
    return {"conversation_id": convo_id, "reply": reply}


@router.get("/conversations", summary="Assistant history")
async def conversations(params: PageParams = Depends(page_params),
                        user: CurrentUser = Depends(get_current_user),
                        db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    return await paginate(db, "ai_conversations", {"user_id": user.id}, params,
                          sort=[("updated_at", -1)])


@router.get("/conversations/{conversation_id}")
async def conversation(conversation_id: str,
                       user: CurrentUser = Depends(get_current_user),
                       db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    doc = await db.ai_conversations.find_one({"_id": oid(conversation_id), "user_id": user.id})
    return serialize(doc)


@router.delete("/conversations/{conversation_id}", response_model=Message)
async def delete_conversation(conversation_id: str,
                              user: CurrentUser = Depends(get_current_user),
                              db: AsyncIOMotorDatabase = Depends(get_tenant_db)):
    await db.ai_conversations.delete_one({"_id": oid(conversation_id), "user_id": user.id})
    return {"detail": "Conversation deleted"}
