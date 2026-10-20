"""NEXUS 平台助手 MCP Server。

OpenCode → MCP 调用时使用服务认证；用户身份由 Backend 注入
（``settings.assistant_user_id``），不信任模型自己提供的 user_id。
MCP 不直接访问数据库表，统一调用 Tool Gateway / Domain Service，
避免形成“助手 SQL”和“正常后端服务”两套业务规则。
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from app.assistant import tool_gateway
from app.assistant.operations import OperationError
from app.assistant.review import ReviewExperience, session_messages, submit_review
from app.database import SessionLocal
from app.library_service import LibraryError


def build_assistant_mcp_server() -> MCPServer:
    server = MCPServer(
        name="NEXUS Review Tools",
        title="NEXUS 复盘工具",
        version="1.0.0",
        description="只读查询任务、员工、运行和知识，并提交任务总结与候选经验。",
        instructions=(
            "查询类工具直接调用。任务经验沉淀使用 nexus_task_review_submit 提交总结和 candidate。"
            "不要创建、取消或重试任务。"
        ),
    )

    async def _run(coro_factory) -> Any:
        async with SessionLocal() as session:
            try:
                return await coro_factory(session)
            except (OperationError, LibraryError, ValueError) as exc:
                raise ToolError(str(exc)) from None

    # ── Task Tools ────────────────────────────────────────────

    @server.tool(name="nexus_task_search", structured_output=True)
    async def nexus_task_search(
        task_id: str | None = None,
        keyword: str | None = None,
        employee_id: str | None = None,
        task_type: str | None = None,
        status: list[str] | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """按任务 ID、关键词、员工、类型和状态搜索平台任务。"""
        return await _run(
            lambda session: tool_gateway.task_search(
                session,
                task_id=task_id,
                keyword=keyword,
                employee_id=employee_id,
                task_type=task_type,
                status=status,
                limit=limit,
            )
        )

    @server.tool(name="nexus_task_get", structured_output=True)
    async def nexus_task_get(task_id: str) -> dict[str, Any]:
        """获取单个任务的完整详情。"""
        return await _run(lambda session: tool_gateway.task_get(session, task_id))

    # 任务创建、取消和重试留在平台自己的接口。复盘执行器不提供这些工具。

    # ── Employee Tools ────────────────────────────────────────

    @server.tool(name="nexus_employee_search", structured_output=True)
    async def nexus_employee_search(keyword: str | None = None, limit: int = 20) -> dict[str, Any]:
        """按名字或岗位搜索 Agent Worker。"""
        return await _run(lambda session: tool_gateway.employee_search(session, keyword=keyword, limit=limit))

    @server.tool(name="nexus_employee_get", structured_output=True)
    async def nexus_employee_get(employee_id: str) -> dict[str, Any]:
        """获取单个 Agent Worker 的详情、环境和当前任务。"""
        return await _run(lambda session: tool_gateway.employee_get(session, employee_id))

    @server.tool(name="nexus_employee_recommend", structured_output=True)
    async def nexus_employee_recommend(
        task_type: str | None = None,
        category_id: str | None = None,
        only_available: bool = True,
    ) -> dict[str, Any]:
        """由平台规则推荐执行员工并给出推荐原因；不要按名字猜测员工。"""
        return await _run(
            lambda session: tool_gateway.employee_recommend(
                session, task_type=task_type, category_id=category_id, only_available=only_available
            )
        )

    # ── Session Tools ─────────────────────────────────────────

    @server.tool(name="nexus_session_search", structured_output=True)
    async def nexus_session_search(
        task_id: str | None = None, status: list[str] | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """搜索任务执行会话。"""
        return await _run(
            lambda session: tool_gateway.session_search(
                session, task_id=task_id, status=status, limit=limit
            )
        )

    @server.tool(name="nexus_session_get", structured_output=True)
    async def nexus_session_get(session_id: str) -> dict[str, Any]:
        """获取单次执行会话详情。"""
        return await _run(lambda session: tool_gateway.session_get(session, session_id))

    @server.tool(name="nexus_session_messages", structured_output=True)
    async def nexus_session_messages(
        session_id: str, before: str | None = None, limit: int = 20,
        native_session_id: str | None = None,
    ) -> dict[str, Any]:
        """只读完整原生会话。分页从新到旧，必须读到 next_before 为空；子会话用 native_session_id。"""
        return await _run(lambda session: session_messages(
            session, session_id, before=before, limit=limit, native_session_id=native_session_id,
        ))

    @server.tool(name="nexus_task_review_submit", structured_output=True)
    async def nexus_task_review_submit(
        task_id: str, source_session_id: str, review_id: str,
        task_summary: str, experiences: list[ReviewExperience],
    ) -> dict[str, Any]:
        """结构化、幂等保存模型复盘与 0～N 条 candidate；review_id 来自会话读取，不改变任务状态。"""
        return await _run(lambda session: submit_review(
            session, task_id=task_id, source_session_id=source_session_id,
            expected_review_id=review_id, task_summary=task_summary, experiences=experiences,
        ))

    @server.tool(name="nexus_experience_search", structured_output=True)
    async def nexus_experience_search(keyword: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """复盘专用：分页检索候选与已发布经验，比较语义后指定 merge_into_id；不发布知识。"""
        return await _run(lambda session: tool_gateway.experience_search(
            session, keyword=keyword, limit=limit, offset=offset,
        ))

    @server.tool(name="nexus_session_failure_summary", structured_output=True)
    async def nexus_session_failure_summary(task_id: str | None = None) -> dict[str, Any]:
        """后端汇总失败现场（结束原因、任务进展），再交给模型解释。"""
        return await _run(
            lambda session: tool_gateway.session_failure_summary(session, task_id=task_id)
        )

    # ── Knowledge Tools ───────────────────────────────────────

    @server.tool(name="nexus_knowledge_search", structured_output=True)
    async def nexus_knowledge_search(keyword: str, limit: int = 10) -> dict[str, Any]:
        """搜索数字图书馆已发布知识。"""
        return await _run(
            lambda session: tool_gateway.knowledge_search(session, keyword=keyword, limit=limit)
        )

    @server.tool(name="nexus_knowledge_get", structured_output=True)
    async def nexus_knowledge_get(knowledge_id: str) -> dict[str, Any]:
        """读取一条知识的完整内容；只根据真实返回内容回答。"""
        return await _run(lambda session: tool_gateway.knowledge_get(session, knowledge_id))

    # ── Operations Tools ──────────────────────────────────────

    @server.tool(name="nexus_operation_overview", structured_output=True)
    async def nexus_operation_overview() -> dict[str, Any]:
        """平台任务整体统计，由数据库计算，不要让模型自己数。"""
        return await _run(lambda session: tool_gateway.operation_overview(session))

    @server.tool(name="nexus_operation_pending_actions", structured_output=True)
    async def nexus_operation_pending_actions() -> dict[str, Any]:
        """待人工、执行异常、待确认等需要关注的任务数量。"""
        return await _run(lambda session: tool_gateway.operation_pending_actions(session))

    @server.tool(name="nexus_operation_employee_load", structured_output=True)
    async def nexus_operation_employee_load() -> dict[str, Any]:
        """各 Agent Worker 当前任务负载。"""
        return await _run(lambda session: tool_gateway.operation_employee_load(session))

    return server


mcp = build_assistant_mcp_server()


def main() -> None:
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
