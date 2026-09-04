from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant.schemas import OperationResponse
from app.assistant.services.confirmation_service import ConfirmationService
from app.database import get_session as get_db

router = APIRouter(prefix="/operations", tags=["Assistant Operations"])

@router.get("/{operation_id}", response_model=OperationResponse)
async def get_operation(operation_id: str, db: AsyncSession = Depends(get_db)):
    service = ConfirmationService(db)
    op = await service.get_operation(operation_id)
    if not op:
        raise HTTPException(status_code=404, detail="Operation not found")
    return op

@router.post("/{operation_id}/confirm", response_model=OperationResponse)
async def confirm_operation(operation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConfirmationService(db)
    try:
        return await service.confirm_operation(operation_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

@router.post("/{operation_id}/cancel", response_model=OperationResponse)
async def cancel_operation(operation_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user_id = request.headers.get("X-Astra-User-Id", "admin")
    service = ConfirmationService(db)
    try:
        return await service.cancel_operation(operation_id, user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
