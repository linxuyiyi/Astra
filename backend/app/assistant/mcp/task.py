from app.assistant.tools import task_tools
from mcp.server.mcpserver import MCPServer as Server


def register_task_tools(server: Server, SessionLocal):
    @server.tool(name="nexus_task_search", structured_output=True)
    async def nexus_task_search(task_id: str | None = None, keyword: str | None = None):
        async with SessionLocal() as db_session:
            return await task_tools.nexus_task_search(
                {"task_id": task_id, "keyword": keyword}, "admin", db_session
            )

    @server.tool(name="nexus_task_get", structured_output=True)
    async def nexus_task_get(task_id: str):
        async with SessionLocal() as db_session:
            return await task_tools.nexus_task_get({"task_id": task_id}, "admin", db_session)

    @server.tool(name="nexus_task_prepare_create", structured_output=True)
    async def nexus_task_prepare_create(
        task_type: str, employee_id: str, title: str | None = None,
        description: str | None = None, category_id: str | None = None,
        acceptance_criteria: list[str] | None = None, priority: str | None = None
    ):
        async with SessionLocal() as db_session:
            return await task_tools.nexus_task_prepare_create(
                {
                    "task_type": task_type,
                    "employee_id": employee_id,
                    "title": title,
                    "description": description,
                    "category_id": category_id,
                    "acceptance_criteria": acceptance_criteria,
                    "priority": priority
                }, "admin", db_session
            )

    @server.tool(name="nexus_task_cancel", structured_output=True)
    async def nexus_task_cancel(task_id: str):
        async with SessionLocal() as db_session:
            return await task_tools.nexus_task_cancel({"task_id": task_id}, "admin", db_session)
