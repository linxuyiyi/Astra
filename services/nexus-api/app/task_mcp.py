"""员工任务 MCP。工具名使用 nexus_task_*，避免和已卸掉的内置 Task* 重名。"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config.settings import get_settings
from app.database import SessionLocal
from app.task_tools import TaskTools


class HealthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path == "/health":
            return JSONResponse({"ok": True})
        return await call_next(request)


def build_mcp_server() -> MCPServer:
    server = MCPServer(
        name="NEXUS Task",
        title="NEXUS 任务",
        version="1.0.1",
        description="Agent Worker 查询和控制自己负责的工程任务。",
        instructions=(
            "工程执行才创建任务。收到任务状态提示后先调用 nexus_task_get。"
            "等待状态先查 nexus_task_assistance；能根据已有上下文回答就调用 nexus_task_reply，"
            "正常回合结束后可通过 nexus_task_input 续答。无法确定或需要新授权时在原对话询问人，"
            "取得明确答案后回传，不要求人去 AgentWork 操作。"
            "总结和候选经验只读。非法操作会返回 ok=false，不要改口说已经成功。"
        ),
    )

    async def _call(name: str, **kwargs):
        async with SessionLocal() as session:
            return await getattr(TaskTools(session), name)(**kwargs)

    @server.tool(name="nexus_task_create", structured_output=True)
    async def nexus_task_create(
        employee_id: str,
        task_type: Literal["ISSUE", "REQUIREMENT"],
        category_id: str,
        title: str,
        description: str,
        acceptance_criteria: list[str] | None = None,
        priority: str = "MEDIUM",
        worker_id: str | None = None,
        repository_id: str | None = None,
        idempotency_key: str | None = None,
        source_session_id: str | None = None,
        source_conversation_id: str | None = None,
        source_channel_id: str | None = None,
    ) -> dict[str, Any]:
        """为 employee_id 指定的数字员工创建工程任务，只检查员工是否存在。

        source_* 为可选的通知回传信息，不用于权限判断。提供来源会话时，相同内容不会重复创建。

        同一工作的补充指令应查询原任务并使用 nexus_task_input，不要重新创建。
        """
        return await _call(
            "create",
            responsible_employee_id=employee_id,
            task_type=task_type,
            category_id=category_id,
            title=title,
            description=description,
            acceptance_criteria=acceptance_criteria,
            priority=priority,
            worker_id=worker_id,
            repository_id=repository_id,
            idempotency_key=idempotency_key,
            source_session_id=source_session_id,
            source_conversation_id=source_conversation_id,
            source_channel_id=source_channel_id,
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
    async def nexus_task_list(
        employee_id: str | None = None, status: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """列出任务，可按责任员工 ID 和状态筛选。"""
        return await _call("list", responsible_employee_id=employee_id, status=status, limit=limit)

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
        """保留原 Task 和执行会话；补充执行指令使用 nexus_task_input，不会重新开始。"""
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

    @server.tool(name="nexus_task_summary", structured_output=True)
    async def nexus_task_summary(task_id: str) -> dict[str, Any]:
        """只读查询任务的真实复盘状态、经验候选和图书馆入口。"""
        return await _call("summary", task_id=task_id)

    @server.tool(name="nexus_task_intervene", structured_output=True)
    async def nexus_task_intervene(task_id: str, reason: str) -> dict[str, Any]:
        """把任务转为等待人工处理，并记下原因。"""
        return await _call("intervene", task_id=task_id, reason=reason)

    @server.tool(name="nexus_task_assistance", structured_output=True)
    async def nexus_task_assistance(task_id: str, cursor: int = 0) -> dict[str, Any]:
        """查询任务执行端的问题、权限和执行输出；next_cursor 非空时继续分页读取。"""
        return await _call("assistance", task_id=task_id, cursor=cursor)

    @server.tool(name="nexus_task_reply", structured_output=True)
    async def nexus_task_reply(
        task_id: str,
        expected_session_id: str,
        request_id: str,
        kind: Literal["question", "permission"],
        answers: list[list[str]] | None = None,
        decision: str | None = None,
    ) -> dict[str, Any]:
        """回复查询所得请求。问题按顺序传 answers；权限选一次性 decision，不能编造授权。"""
        return await _call(
            "reply",
            task_id=task_id,
            expected_session_id=expected_session_id,
            request_id=request_id,
            kind=kind,
            answers=answers,
            decision=decision,
        )

    @server.tool(name="nexus_task_input", structured_output=True)
    async def nexus_task_input(
        task_id: str,
        expected_session_id: str,
        expected_submission_id: str,
        text: str,
    ) -> dict[str, Any]:
        """依据求助查询返回的执行会话和回合标识，向同一 OpenCode 会话发送补充执行指令，不新建任务。"""
        return await _call(
            "input",
            task_id=task_id,
            expected_session_id=expected_session_id,
            expected_submission_id=expected_submission_id,
            text=text,
        )

    @server.tool(name="nexus_task_confirm", structured_output=True)
    async def nexus_task_confirm(task_id: str) -> dict[str, Any]:
        """确认整个业务任务完成，须核实完整目标与验收要求均已达成。

        单轮结束或阶段检视通过不等于任务完成，有后续工作时使用 nexus_task_input。
        """
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
    app.add_middleware(HealthMiddleware)
    uvicorn.run(app, host=settings.task_mcp_host, port=settings.task_mcp_port)


if __name__ == "__main__":
    main()
