"""统一 Worker 配置接口；认证摘要、SSH 和 Git 密文均不返回。"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.api.routes import validate_category_ids
from app.config.settings import get_settings
from app.database import get_session
from app.domain import WorkerStatus
from app.models import (
    AgentWorker,
    Event,
    KnowledgeFeedback,
    KnowledgeItem,
    KnowledgeUsage,
    Task,
    WorkerHttpSlot,
    WorkerMaintenanceOperation,
)
from app.runtime.occupancy import occupying_exists, sync_worker_occupancy
from app.runtime.repository import cipher
from app.schemas import WorkerCreate, WorkerRead, WorkerWrite

router = APIRouter(prefix="/api/workers")


async def require_worker(worker_id, session):
    worker = await session.scalar(select(AgentWorker).where(AgentWorker.id == worker_id).with_for_update())
    if not worker:
        raise HTTPException(404, "Worker 不存在")
    return worker


async def guard_worker(session, worker):
    occupied = await session.scalar(select(1).where(occupying_exists(worker.id)))
    maintenance = await session.scalar(
        select(WorkerMaintenanceOperation.id).where(WorkerMaintenanceOperation.active_worker_id == worker.id)
    )
    if occupied or maintenance:
        raise HTTPException(409, "Worker 有未释放任务或维护操作，不能修改执行配置")


def config_values(payload):
    data = payload.model_dump(mode="json")
    for key in ("host", "username", "model_name", "repository_url", "branch"):
        data[key] = data[key] or None
    password, token = data.pop("ssh_password"), data.pop("git_token")
    if password is not None:
        data["ssh_password"] = cipher().encrypt(password) if password else None
    if token is not None:
        data["git_token_ciphertext"] = cipher().encrypt(token) if token else None
    return data


@router.get("", response_model=list[WorkerRead])
async def list_workers(
    response: Response,
    status: WorkerStatus | None = None,
    q: str = "",
    limit: int = Query(200, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session=Depends(get_session),
):
    query = select(AgentWorker)
    if status:
        query = query.where(AgentWorker.status == status)
    if keyword := q.strip():
        query = query.where(AgentWorker.name.ilike(f"%{keyword}%") | AgentWorker.host.ilike(f"%{keyword}%"))
    response.headers["X-Total-Count"] = str(
        await session.scalar(select(func.count()).select_from(query.subquery()))
    )
    return (
        await session.scalars(
            query.order_by(AgentWorker.created_at, AgentWorker.id).offset(offset).limit(limit)
        )
    ).all()


@router.post("", response_model=WorkerRead, status_code=201)
async def create_worker(payload: WorkerCreate, session=Depends(get_session)):
    await validate_category_ids(payload.category_ids, session)
    worker = AgentWorker(**config_values(payload))
    if worker.mode == "LOCAL" and get_settings().executor_type == "opencode_server":
        worker.status = "IDLE"
    session.add(worker)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(409, "Worker ID 已存在") from exc
    return worker


@router.get("/{worker_id}", response_model=WorkerRead)
async def get_worker(worker_id: str, session=Depends(get_session)):
    return await require_worker(worker_id, session)


@router.put("/{worker_id}", response_model=WorkerRead)
async def update_worker(worker_id: str, payload: WorkerWrite, session=Depends(get_session)):
    worker = await require_worker(worker_id, session)
    await validate_category_ids(payload.category_ids, session)
    values = config_values(payload)
    execution_fields = {
        "runtime_engine",
        "mode",
        "host",
        "port",
        "username",
        "ssh_password",
        "model_name",
        "workspace_root",
        "repository_url",
        "git_token_ciphertext",
        "branch",
        "skill_refs",
        "mcp_refs",
    }
    changed = any(key in execution_fields and value != getattr(worker, key) for key, value in values.items())
    previous_epoch = worker.epoch
    if changed:
        await guard_worker(session, worker)
        worker.token_digest = None
        worker.epoch += 1
        worker.last_heartbeat_at = None
        worker.status = "PAUSED" if worker.status == "PAUSED" else "OFFLINE"
    for key, value in values.items():
        setattr(worker, key, value)
    await session.commit()
    if changed:
        from app.worker_gateway.service import get_gateway

        gateway = get_gateway()
        await gateway.bus.clear_owner(worker.id, previous_epoch, None)
        gateway._close_local_socket(worker.id, previous_epoch)
    return worker


@router.post("/{worker_id}/pause", response_model=WorkerRead)
async def pause_worker(worker_id: str, session=Depends(get_session)):
    worker = await require_worker(worker_id, session)
    worker.status = "PAUSED"
    await session.commit()
    return worker


@router.post("/{worker_id}/resume", response_model=WorkerRead)
async def resume_worker(worker_id: str, session=Depends(get_session)):
    from app.worker_gateway.service import get_gateway

    worker = await require_worker(worker_id, session)
    owner = await get_gateway().bus.get_owner(worker.id)
    local = worker.mode == "LOCAL" and get_settings().executor_type == "opencode_server"
    worker.status = "IDLE" if local or (owner and owner[1] == worker.epoch) else "OFFLINE"
    await sync_worker_occupancy(session, worker.id)
    await session.commit()
    return worker


@router.delete("/{worker_id}")
async def delete_worker(worker_id: str, session=Depends(get_session)):
    worker = await require_worker(worker_id, session)
    await guard_worker(session, worker)
    await session.execute(update(Task).where(Task.agent_worker_id == worker_id).values(agent_worker_id=None))
    await session.execute(
        update(KnowledgeItem)
        .where(KnowledgeItem.source_employee_id == worker_id)
        .values(source_employee_id=None)
    )
    for model in (Event, KnowledgeUsage, KnowledgeFeedback):
        await session.execute(update(model).where(model.employee_id == worker_id).values(employee_id=None))
    previous_epoch = worker.epoch
    await session.execute(delete(WorkerHttpSlot).where(WorkerHttpSlot.agent_worker_id == worker_id))
    await session.delete(worker)
    await session.commit()
    from app.worker_gateway.service import get_gateway

    gateway = get_gateway()
    await gateway.bus.clear_owner(worker_id, previous_epoch, None)
    gateway._close_local_socket(worker_id, previous_epoch)
    return {"deleted": worker_id}
