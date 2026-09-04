from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.services.assistant_service import AssistantService
from app.assistant.services.conversation_service import ConversationService
from app.database import get_session as get_db

router = APIRouter(prefix="/conversations/{conversation_id}/events", tags=["Assistant Events"])

@router.get("")
async def stream_events(conversation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    conv = await ConversationService(db).get_conversation(conversation_id)
    if not conv or conv.user_id != user_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    service = AssistantService(db)
    return StreamingResponse(service.stream_events(conversation_id, request), media_type="text/event-stream")
