from app.assistant.tools import operation_tools
from mcp.server.mcpserver import MCPServer as Server


def register_operation_tools(server: Server, SessionLocal):
    @server.tool(name="nexus_operation_overview", structured_output=True)
    async def nexus_operation_overview(
        start_time: str | None = None, end_time: str | None = None
    ):
        async with SessionLocal() as db_session:
            return await operation_tools.nexus_operation_overview(
                {"start_time": start_time, "end_time": end_time}, "admin", db_session
            )
