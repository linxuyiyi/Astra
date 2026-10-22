"""日程 MCP：私聊员工的创建/查看工具，归属由会话头交给 Nexus 解析。"""

from __future__ import annotations

import hmac
import os
from contextvars import ContextVar
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

SESSION_ID: ContextVar[str] = ContextVar("schedule_mcp_session_id", default="")
EMPLOYEE_ID: ContextVar[str] = ContextVar("schedule_mcp_employee_id", default="")
CONVERSATION_ID: ContextVar[str] = ContextVar("schedule_mcp_conversation_id", default="")


def presented_bearer(header: str | None) -> str:
    value = (header or "").strip()
    if len(value) < 7 or value[:7].lower() != "bearer ":
        return ""
    return value[7:].strip()


def authenticate(headers) -> str:
    """共享密钥证明请求来自 Nexus 挂接的 Session；会话和员工必须同时存在。"""

    expected = os.environ.get("NEXUS_SCHEDULE_MCP_KEY", "").strip()
    presented = presented_bearer(headers.get("authorization"))
    if not expected or not presented or not hmac.compare_digest(presented, expected):
        raise PermissionError("unauthorized")
    employee = (headers.get("x-nexus-employee-id") or "").strip()
    session = (headers.get("x-nexus-session-id") or "").strip()
    if not employee or not session:
        raise PermissionError("missing identity")
    return employee


class ScheduleIdentityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"ok": True})
        try:
            authenticate(request.headers)
        except PermissionError:
            return JSONResponse({"ok": False, "error": "未授权"}, status_code=401)
        tokens = [
            (SESSION_ID, SESSION_ID.set(request.headers.get("x-nexus-session-id", ""))),
            (EMPLOYEE_ID, EMPLOYEE_ID.set(request.headers.get("x-nexus-employee-id", ""))),
            (
                CONVERSATION_ID,
                CONVERSATION_ID.set(request.headers.get("x-nexus-conversation-id", "")),
            ),
        ]
        try:
            return await call_next(request)
        finally:
            for variable, token in tokens:
                variable.reset(token)


def nexus_api_base() -> str:
    return os.environ.get("NEXUS_API_BASE", "http://127.0.0.1:18120").rstrip("/")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=nexus_api_base(), timeout=20)


async def _call(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    key = os.environ.get("NEXUS_INTERNAL_KEY", "").strip()
    if not key:
        return {"ok": False, "error": "内部日程接口未配置"}
    headers = {
        "X-Nexus-Internal-Key": key,
        "X-Nexus-Session-Id": SESSION_ID.get(),
        "X-Nexus-Employee-Id": EMPLOYEE_ID.get(),
        "X-Nexus-Conversation-Id": CONVERSATION_ID.get(),
    }
    try:
        async with _client() as client:
            response = await client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError:
        return {"ok": False, "error": "日程服务不可达，请稍后重试"}
    if response.status_code >= 400:
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
            detail = payload["detail"]
        return {"ok": False, "error": detail or f"日程服务返回 {response.status_code}"}
    payload = response.json()
    if not isinstance(payload, dict):
        return {"ok": False, "error": "日程服务响应无效"}
    return {"ok": True, "data": payload}


async def create_schedule(
    name: str,
    description: str,
    cron_expression: str,
    timezone: str = "Asia/Shanghai",
    stateful: bool = False,
    enabled: bool = False,
) -> dict[str, Any]:
    """在当前人类名下创建周期日程；默认不启用，等人类到日程页确认。"""

    result = await _call(
        "POST",
        "/api/v1/internal/schedules",
        json={
            "name": name,
            "description": description,
            "cron_expression": cron_expression,
            "timezone": timezone,
            "stateful": stateful,
            "enabled": enabled,
        },
    )
    if not result["ok"]:
        return result
    return {"ok": True, "schedule": result["data"]}


async def list_schedules() -> dict[str, Any]:
    """列出当前人类在本次员工下的日程。"""

    result = await _call("GET", "/api/v1/internal/schedules")
    if not result["ok"]:
        return result
    payload = result["data"]
    return {
        "ok": True,
        "schedules": payload.get("items", []),
        "total": payload.get("total", 0),
    }


async def view_schedule(schedule_id: str) -> dict[str, Any]:
    """查看当前人类在本次员工下的一个日程。"""

    result = await _call("GET", f"/api/v1/internal/schedules/{schedule_id}")
    if not result["ok"]:
        return result
    return {"ok": True, "schedule": result["data"]}


def build_server() -> MCPServer:
    server = MCPServer(
        name="nexus-schedule",
        title="NEXUS 日程",
        version="1.0.0",
        description="数字员工在私聊中创建、查看当前人类的周期日程。",
        instructions=(
            "只有私聊会提供这些工具。先确认周期、时区、到点后要做什么，再创建日程。"
            "cron 用五字段（分 时 日 月 周），时区默认 Asia/Shanghai。"
            "创建默认不启用；告诉人类到日程安排页确认后启用。"
            "团队、邮箱和日程执行会话没有这些工具，不要假装已创建。"
        ),
    )

    @server.tool(name="schedule_create", structured_output=True)
    async def schedule_create(
        name: str,
        description: str,
        cron_expression: str,
        timezone: str = "Asia/Shanghai",
        stateful: bool = False,
        enabled: bool = False,
    ) -> dict[str, Any]:
        """创建周期日程。

        name：日程名称。description：到点后要做什么、期望产出和必要上下文；
        这是执行时唯一的任务说明。cron_expression：五字段 cron。
        timezone：IANA 时区，默认 Asia/Shanghai。stateful：连续执行是否复用上下文。
        enabled：默认 false；人类明确要求立即启用时才传 true。
        """

        return await create_schedule(name, description, cron_expression, timezone, stateful, enabled)

    @server.tool(name="schedule_list", structured_output=True)
    async def schedule_list() -> dict[str, Any]:
        """列出当前人类在本次员工下的日程。"""

        return await list_schedules()

    @server.tool(name="schedule_view", structured_output=True)
    async def schedule_view(schedule_id: str) -> dict[str, Any]:
        """按日程标识查看详情。"""

        return await view_schedule(schedule_id)

    return server


def build_app():
    host = os.environ.get("NEXUS_SCHEDULE_MCP_HOST", "127.0.0.1")
    app = build_server().streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=host,
    )
    app.add_middleware(ScheduleIdentityMiddleware)
    return app


def main() -> None:
    import uvicorn

    host = os.environ.get("NEXUS_SCHEDULE_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("NEXUS_SCHEDULE_MCP_PORT", "18203"))
    uvicorn.run(build_app(), host=host, port=port)


if __name__ == "__main__":
    main()
