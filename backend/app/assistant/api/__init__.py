from fastapi import APIRouter

from .conversations import router as conversations_router
from .events import router as events_router
from .messages import router as messages_router
from .operations import router as operations_router

router = APIRouter(prefix="/api/assistant")
router.include_router(conversations_router)
router.include_router(messages_router)
router.include_router(operations_router)
router.include_router(events_router)
