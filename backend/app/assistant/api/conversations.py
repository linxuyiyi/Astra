from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.schemas import ConversationCreate, ConversationResponse
from app.assistant.services.conversation_service import ConversationService
from app.database import get_session as get_db

router = APIRouter(prefix="/conversations", tags=["Assistant Conversations"])



@router.post("", response_model=ConversationResponse)

@router.post("", response_model=ConversationResponse)
async def create_conversation(
    conversation_in: ConversationCreate, request: Request, db: AsyncSession = Depends(get_db)
):
    # user_id should be extracted from context/auth
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConversationService(db)
    return await service.create_conversation(user_id, conversation_in)

@router.get("", response_model=list[ConversationResponse])
async def list_conversations(request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConversationService(db)
    return await service.list_conversations(user_id)

@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConversationService(db)
    conv = await service.get_conversation(conversation_id)
    if not conv or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv

@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConversationService(db)
    conv = await service.get_conversation(conversation_id)
    if not conv or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await service.delete_conversation(conversation_id)
    return {"status": "ok"}
