"""Task + binding + native Session 三重隔离的通用 DSH 工作台。"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.routes import require_task
from app.database import get_session
from app.models import RuntimeSession
from app.runtime.dsh_client import DshRuntimeClient
from app.runtime.dsh_lifecycle import DshLifecycle
from app.runtime.dsh_projection import project_event
from app.runtime.lifecycle import RuntimeService
from app.runtime.redaction import redact_data, redact_text
from app.runtime.session_connection import resolve_runtime_session_environment
from app.runtime.worker_files import worker_read_text
from app.runtime.workspace_changes import workspace_diff

router = APIRouter(prefix="/api/tasks/{task_id}/workbench/{binding_id}")


async def context(task_id, binding_id, native_session_id, db, *, write=False, allow_ended=False):
    task = await require_task(task_id, db)
    binding = await db.get(RuntimeSession, binding_id)
    if (
        not binding
        or binding.task_id != task.id
        or binding.runtime_engine != "dsh"
        or not native_session_id
        or binding.native_session_id != native_session_id
    ):
        raise HTTPException(404, "Task、绑定或原生 Session 不匹配")
    if write and (
        task.current_session_id != binding.id
        or (not allow_ended and task.status in {"SUCCEEDED", "CANCELED"})
    ):
        raise HTTPException(409, "历史或已结束任务只读")
    environment = await resolve_runtime_session_environment(db, binding)
    client = DshRuntimeClient(environment.id, binding)
    await db.commit()
    return task, binding, environment, client


async def guarded(awaitable):
    from app.api.workspace_routes import error_response

    try:
        return await awaitable
    except Exception as exc:
        raise error_response(exc) from exc


@router.get("")
async def workbench(
    task_id: str,
    binding_id: str,
    native_session_id: str,
    after: int = Query(0, ge=0),
    session=Depends(get_session),
):
    task, binding, _, client = await context(task_id, binding_id, native_session_id, session)
    snapshot = await guarded(client.snapshot(after=after))
    # 事件序号只在该 Worker stream 中有效；刷新历史时使用持久轨迹 seq。
    return redact_data(
        {
            "task_id": task.id,
            "binding_id": binding.id,
            "native_session_id": native_session_id,
            "current": task.current_session_id == binding.id,
            "worker": {
                "id": binding.employee_id,
                "instance_id": snapshot.get("worker_id"),
                "name": binding.environment.get("name"),
            },
            "task_state": {
                "status": task.status,
                "waiting_reason": task.waiting_reason,
                "connection": task.runtime_connection,
                "pending_submission": task.pending_message_id,
                "binding_id": task.current_session_id,
                "session_status": binding.status,
            },
            "snapshot": snapshot,
            "capabilities": {
                "messages": True,
                "tools": True,
                "permissions": True,
                "files": True,
                "diff": True,
                "reasoning": False,
                "terminal": False,
                "plans": False,
                "todos": False,
            },
        }
    )


@router.get("/trace")
async def trace(
    task_id: str,
    binding_id: str,
    native_session_id: str,
    cursor: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=100),
    session=Depends(get_session),
):
    _, _, _, client = await context(task_id, binding_id, native_session_id, session)
    page = await guarded(client.read_trace_page(cursor=cursor, limit=limit))
    # 浏览器不接收原生 replayState/完整 reasoning 或内部运行上下文。
    page["events"] = [
        value for event in page["events"] if (value := project_event(event, native_session_id)) is not None
    ]
    return redact_data(page)


class Input(BaseModel):
    native_session_id: str
    text: str = Field(min_length=1, max_length=100000)


@router.post("/input")
async def submit(task_id: str, binding_id: str, body: Input, session=Depends(get_session)):
    await context(task_id, binding_id, body.native_session_id, session, write=True)
    await guarded(
        DshLifecycle(RuntimeService(session)).send(task_id, body.text, expected_binding_id=binding_id)
    )
    return {"accepted": True}


class Permission(BaseModel):
    native_session_id: str
    permission_id: str
    option_id: str


@router.post("/permission")
async def permission(task_id: str, binding_id: str, body: Permission, session=Depends(get_session)):
    _, _, _, client = await context(task_id, binding_id, body.native_session_id, session, write=True)
    await guarded(client.reply_permission(permission_id=body.permission_id, option_id=body.option_id))
    return {"accepted": True}


class Action(BaseModel):
    native_session_id: str
    action: Literal["stop", "stop-current", "confirm", "continue", "restart", "reconnect"]


@router.post("/action")
async def action(task_id: str, binding_id: str, body: Action, session=Depends(get_session)):
    await context(
        task_id, binding_id, body.native_session_id, session,
        write=True, allow_ended=body.action == "restart",
    )
    runtime = RuntimeService(session)
    driver = DshLifecycle(runtime)
    if body.action in {"stop", "stop-current"}:
        await guarded(driver.stop(task_id, cancel_task=body.action == "stop", expected_binding_id=binding_id))
    elif body.action == "confirm":
        await guarded(driver.confirm(task_id, expected_binding_id=binding_id))
    elif body.action == "restart":
        await guarded(runtime.start(task_id, restart=True, expected_session_id=binding_id))
    elif body.action == "continue":
        await guarded(runtime.continue_task(task_id))
    else:
        await guarded(runtime.refresh(task_id))
    return {"accepted": True}


@router.get("/diff")
async def diff(
    task_id: str, binding_id: str, native_session_id: str, mode: str = "task", session=Depends(get_session)
):
    if mode not in {"task", "git"}:
        raise HTTPException(422, "不支持的 Diff 模式")
    task, binding, environment, _ = await context(task_id, binding_id, native_session_id, session)
    return redact_data(
        await guarded(
            workspace_diff(
                session,
                task,
                cumulative=mode == "task",
                directory=binding.workspace,
                environment=environment,
                workspace=binding.workspace,
            )
        )
    )


@router.get("/file")
async def file(
    task_id: str, binding_id: str, native_session_id: str, path: str, session=Depends(get_session)
):
    _, binding, environment, _ = await context(task_id, binding_id, native_session_id, session)
    return {
        "path": path,
        "content": redact_text(
            await guarded(
                worker_read_text(environment, binding.workspace, path),
            )
        ),
    }
