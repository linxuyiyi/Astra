"""仓库登记、绑定、克隆和 Worker 复制。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.gateway.security import CredentialError
from app.models import CodeRepository, GitCredential, Task
from app.runtime.repository import (
    CloneError,
    RepositoryBindingError,
    bind_repository,
    cipher,
    clone_repository,
    copy_worker,
    create_repository,
)

router = APIRouter(prefix="/api")


class RepositoryCreate(BaseModel):
    url: str = Field(min_length=1)
    branch: str = "main"
    token: str = Field(min_length=1)


class RepositoryRead(BaseModel):
    id: str
    url: str
    branch: str


class RepositoryBind(BaseModel):
    repository_id: str = Field(min_length=1)


class RepositoryClone(BaseModel):
    destination: str = Field(min_length=1)


class WorkerCopy(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    port: int | None = Field(default=None, ge=1, le=65535)


def _binding_error(exc: RepositoryBindingError) -> HTTPException:
    status_code = 404 if str(exc) == "仓库不存在" else 409
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/repositories", response_model=list[RepositoryRead])
async def list_repositories(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.scalars(
            select(CodeRepository).order_by(CodeRepository.created_at.desc())
        )
    ).all()
    return [RepositoryRead(id=row.id, url=row.url, branch=row.branch) for row in rows]


@router.post("/repositories", response_model=RepositoryRead, status_code=201)
async def register_repository(payload: RepositoryCreate, session: AsyncSession = Depends(get_session)):
    try:
        repository = await create_repository(
            session, url=payload.url, branch=payload.branch, token=payload.token
        )
    except CloneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RepositoryRead(id=repository.id, url=repository.url, branch=repository.branch)


@router.post("/repositories/{repository_id}/clone")
async def clone_registered_repository(
    repository_id: str,
    payload: RepositoryClone,
    session: AsyncSession = Depends(get_session),
):
    from app.models import CodeRepository

    repository = await session.get(CodeRepository, repository_id)
    if repository is None:
        raise HTTPException(status_code=404, detail="仓库不存在")
    credential = await session.scalar(
        select(GitCredential).where(GitCredential.repository_id == repository.id)
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="仓库没有凭据")
    from starlette.concurrency import run_in_threadpool

    try:
        token = cipher().decrypt(credential.token_ciphertext)
        await run_in_threadpool(
            clone_repository, repository.url, repository.branch, payload.destination, token
        )
    except CredentialError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except CloneError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/agent-workers/{employee_id}/repository")
async def bind_employee_repository(
    employee_id: str,
    payload: RepositoryBind,
    session: AsyncSession = Depends(get_session),
):
    from app.api.routes import employee_read, require_employee

    employee = await require_employee(employee_id, session)
    environment = employee.execution_environment
    if environment is None:
        raise HTTPException(status_code=409, detail="Agent Worker 尚未配置执行环境")
    try:
        await bind_repository(session, environment, payload.repository_id)
    except RepositoryBindingError as exc:
        raise _binding_error(exc) from exc
    return await employee_read(await require_employee(employee_id, session), session)


@router.post("/agent-workers/{employee_id}/copy", status_code=201)
async def copy_employee(
    employee_id: str,
    payload: WorkerCopy,
    session: AsyncSession = Depends(get_session),
):
    from app.api.routes import employee_read, require_employee

    source = await require_employee(employee_id, session)
    task_count = await session.scalar(
        select(func.count()).select_from(Task).where(Task.employee_id == source.id)
    )
    try:
        await copy_worker(session, source, new_id=payload.id, name=payload.name, port=payload.port)
    except RepositoryBindingError as exc:
        raise _binding_error(exc) from exc
    copied = await employee_read(await require_employee(payload.id, session), session)
    copied_tasks = await session.scalar(
        select(func.count()).select_from(Task).where(Task.employee_id == payload.id)
    )
    return {
        "employee": copied,
        "source_task_count": task_count or 0,
        "copied_task_count": copied_tasks or 0,
    }
