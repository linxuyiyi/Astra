"""Worker 是历史执行员工的只读别名。部署仍走原环境接口。"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes import get_employee, list_employees
from app.database import get_session
from app.schemas import EmployeeRead

router = APIRouter(prefix="/api/workers")


@router.get("", response_model=list[EmployeeRead])
async def list_workers(session: AsyncSession = Depends(get_session)):
    return await list_employees(session)


@router.get("/{worker_id}", response_model=EmployeeRead)
async def get_worker(worker_id: str, session: AsyncSession = Depends(get_session)):
    return await get_employee(worker_id, session)
