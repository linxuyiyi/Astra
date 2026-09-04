from app.assistant.tools import employee_tools
from mcp.server.mcpserver import MCPServer as Server


def register_employee_tools(server: Server, SessionLocal):
    @server.tool(name="nexus_employee_search", structured_output=True)
    async def nexus_employee_search(keyword: str | None = None):
        async with SessionLocal() as db_session:
            return await employee_tools.nexus_employee_search({"keyword": keyword}, "admin", db_session)

    @server.tool(name="nexus_employee_recommend", structured_output=True)
    async def nexus_employee_recommend(task_type: str):
        async with SessionLocal() as db_session:
            return await employee_tools.nexus_employee_recommend(
                {"task_type": task_type}, "admin", db_session
            )
