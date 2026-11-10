"""独立 Task MCP 的内部 HTTP 入口；Runtime 和 SSH 只在统一 API 执行。"""

from typing import Any, Literal

from fastapi import APIRouter, Body, Depends

from app.database import get_session
from app.task_tools import TaskTools
from nexus_api.auth import require_internal

TaskToolMethod = Literal[
    "create", "categories", "get", "list", "update", "assign", "start", "pause", "resume",
    "terminate", "retry", "logs", "result", "summary", "intervene", "assistance", "reply", "input", "confirm",
]

router = APIRouter(prefix="/api/v1/internal/task-tools", dependencies=[Depends(require_internal)])


@router.post("/{method}")
async def invoke_task_tool(
    method: TaskToolMethod, arguments: dict[str, Any] = Body(...), session=Depends(get_session),
):
    return await getattr(TaskTools(session), method)(**arguments)
