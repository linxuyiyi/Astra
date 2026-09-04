from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.schemas import MessageCreate, MessageResponse
from app.assistant.services.assistant_service import AssistantService
from app.assistant.services.conversation_service import ConversationService
from app.database import get_session as get_db

router = APIRouter(prefix="/conversations/{conversation_id}/messages", tags=["Assistant Messages"])

@router.post("", response_model=MessageResponse)
async def create_message(
    conversation_id: str, message_in: MessageCreate, request: Request, db: AsyncSession = Depends(get_db)
):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = AssistantService(db)
    try:
        return await service.send_message(conversation_id, user_id, message_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.get("", response_model=list[MessageResponse])
async def list_messages(conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    conv = await ConversationService(db).get_conversation(conversation_id)
    if not conv or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    service = AssistantService(db)
    return await service.list_messages(conversation_id)

@router.post("/abort")
async def abort_message(conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    conv = await ConversationService(db).get_conversation(conversation_id)
    if not conv or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    service = AssistantService(db)
    await service.abort_generation(conversation_id)
    return {"status": "ok"}
