"""员工任务 MCP。工具名使用 nexus_task_*，避免和已卸掉的内置 Task* 重名。"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config.settings import Settings, get_settings
from app.database import SessionLocal
from app.task_tools import (
    TaskTools,
    nexus_actor_id,
    nexus_channel_id,
    nexus_conversation_id,
    nexus_discussion_id,
    nexus_employee_id,
    nexus_session_id,
)


def presented_bearer(header: str | None) -> str:
    value = (header or "").strip()
    if len(value) < 7 or value[:7].lower() != "bearer ":
        return ""
    return value[7:].strip()


def authenticate_mcp(headers, settings: Settings) -> str:
    """共享密钥证明请求来自 Nexus 挂接的 Session。员工 id 必须同时存在。"""

    expected = (settings.task_mcp_key or "").strip()
    presented = presented_bearer(headers.get("authorization"))
    if not expected or not presented or not hmac.compare_digest(presented, expected):
        raise PermissionError("unauthorized")
    employee = (headers.get("x-nexus-employee-id") or "").strip()
    if not employee:
        raise PermissionError("missing employee")
    return employee


class NexusIdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"ok": True})
        try:
            employee = authenticate_mcp(request.headers, get_settings())
        except PermissionError:
            return JSONResponse({"ok": False, "error": "未授权"}, status_code=401)
        from nexus_api.group_task_auth import authorize_group_task

        try:
            actor = authorize_group_task(
                request.headers.get("x-nexus-session-id", ""),
                employee,
                request.headers.get("x-nexus-channel-id", ""),
                request.headers.get("x-nexus-discussion-id", ""),
            )
        except PermissionError:
            return JSONResponse({"ok": False, "error": "群调用未授权"}, status_code=403)
        tokens = [
            (nexus_discussion_id, nexus_discussion_id.set(request.headers.get("x-nexus-discussion-id", ""))),
            (nexus_actor_id, nexus_actor_id.set(actor)),
            (nexus_employee_id, nexus_employee_id.set(employee)),
            (nexus_session_id, nexus_session_id.set(request.headers.get("x-nexus-session-id", ""))),
            (
                nexus_conversation_id,
                nexus_conversation_id.set(request.headers.get("x-nexus-conversation-id", "")),
            ),
            (nexus_channel_id, nexus_channel_id.set(request.headers.get("x-nexus-channel-id", ""))),
        ]
        try:
            return await call_next(request)
        finally:
            for variable, token in tokens:
                variable.reset(token)


def build_mcp_server() -> MCPServer:
    server = MCPServer(
        name="NEXUS Task",
        title="NEXUS 任务",
        version="1.0.1",
        description="Agent Worker 查询和控制自己负责的工程任务。",
        instructions=(
            "工程执行才创建任务。收到任务状态提示后先调用 nexus_task_get。"
            "总结和候选经验只读。非法操作会返回 ok=false，不要改口说已经成功。"
        ),
    )

    async def _call(name: str, **kwargs):
        if nexus_session_id.get():
            from nexus_api.group_task_auth import authorize_group_task

            try:
                authorize_group_task(
                    nexus_session_id.get(),
                    nexus_employee_id.get(),
                    nexus_channel_id.get(),
                    nexus_discussion_id.get(),
                )
            except PermissionError:
                return {"ok": False, "error": "群调用未授权"}
        async with SessionLocal() as session:
            return await getattr(TaskTools(session), name)(**kwargs)

    @server.tool(name="nexus_task_create", structured_output=True)
    async def nexus_task_create(
        task_type: Literal["ISSUE", "REQUIREMENT"],
        category_id: str,
        title: str,
        description: str,
        acceptance_criteria: list[str] | None = None,
        priority: str = "MEDIUM",
        worker_id: str | None = None,
        repository_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """创建工程任务。相同会话和相同内容不会再建第二条。"""
        return await _call(
            "create",
            task_type=task_type,
            category_id=category_id,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            worker_id=worker_id,
            repository_id=repository_id,
            idempotency_key=idempotency_key,
        )

    @server.tool(name="nexus_task_repositories", structured_output=True)
    async def nexus_task_repositories() -> dict[str, Any]:
        """查询已登记的仓库 ID、地址与分支，不返回凭据。创建任务必须选仓库或已绑定仓库的 Worker。"""
        return await _call("repositories")

    @server.tool(name="nexus_task_categories", structured_output=True)
    async def nexus_task_categories() -> dict[str, Any]:
        """查询已启用的任务分类。创建任务前先选取返回的分类 ID，不猜分类。"""
        return await _call("categories")

    @server.tool(name="nexus_task_get", structured_output=True)
    async def nexus_task_get(task_id: str) -> dict[str, Any]:
        """查询任务最新状态。收到状态提示后先调用这个工具。"""
        return await _call("get", task_id=task_id)

    @server.tool(name="nexus_task_list", structured_output=True)
    async def nexus_task_list(status: str | None = None, limit: int = 20) -> dict[str, Any]:
        """列出当前责任员工的任务。"""
        return await _call("list", status=status, limit=limit)

    @server.tool(name="nexus_task_update", structured_output=True)
    async def nexus_task_update(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        """修改未结束任务的标题、说明或验收标准，不改变状态。"""
        return await _call(
            "update",
            task_id=task_id,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
        )

    @server.tool(name="nexus_task_assign", structured_output=True)
    async def nexus_task_assign(task_id: str, worker_id: str) -> dict[str, Any]:
        """把待领取任务指定给一个 Agent Worker。"""
        return await _call("assign", task_id=task_id, worker_id=worker_id)

    @server.tool(name="nexus_task_start", structured_output=True)
    async def nexus_task_start(task_id: str) -> dict[str, Any]:
        """开始待领取任务。"""
        return await _call("start", task_id=task_id)

    @server.tool(name="nexus_task_pause", structured_output=True)
    async def nexus_task_pause(task_id: str) -> dict[str, Any]:
        """暂停执行中的任务，状态变为等待。"""
        return await _call("pause", task_id=task_id)

    @server.tool(name="nexus_task_resume", structured_output=True)
    async def nexus_task_resume(task_id: str) -> dict[str, Any]:
        """从等待状态恢复任务。"""
        return await _call("resume", task_id=task_id)

    @server.tool(name="nexus_task_terminate", structured_output=True)
    async def nexus_task_terminate(task_id: str) -> dict[str, Any]:
        """终止未结束的任务。"""
        return await _call("terminate", task_id=task_id)

    @server.tool(name="nexus_task_retry", structured_output=True)
    async def nexus_task_retry(task_id: str) -> dict[str, Any]:
        """重试失败或已终止的任务。"""
        return await _call("retry", task_id=task_id)

    @server.tool(name="nexus_task_logs", structured_output=True)
    async def nexus_task_logs(task_id: str) -> dict[str, Any]:
        """读取任务事件。"""
        return await _call("logs", task_id=task_id)

    @server.tool(name="nexus_task_result", structured_output=True)
    async def nexus_task_result(task_id: str) -> dict[str, Any]:
        """读取任务结果摘要。"""
        return await _call("result", task_id=task_id)

    @server.tool(name="nexus_task_intervene", structured_output=True)
    async def nexus_task_intervene(task_id: str, reason: str) -> dict[str, Any]:
        """把任务转为等待人工处理，并记下原因。"""
        return await _call("intervene", task_id=task_id, reason=reason)

    @server.tool(name="nexus_task_confirm", structured_output=True)
    async def nexus_task_confirm(task_id: str) -> dict[str, Any]:
        """确认任务完成。只对执行中或等待确认的任务有效。"""
        return await _call("confirm", task_id=task_id)

    return server


mcp = build_mcp_server()


def main() -> None:
    import uvicorn

    settings = get_settings()
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.task_mcp_host,
    )
    app.add_middleware(NexusIdentityMiddleware)
    uvicorn.run(app, host=settings.task_mcp_host, port=settings.task_mcp_port)


if __name__ == "__main__":
    main()
