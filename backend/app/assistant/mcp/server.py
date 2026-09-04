import asyncio

from app.database import SessionLocal
from mcp.server.mcpserver import MCPServer as Server
from mcp.server.stdio import stdio_server

from .employee import register_employee_tools
from .knowledge import register_knowledge_tools
from .operations import register_operation_tools
from .run import register_run_tools
from .task import register_task_tools


class AssistantMCPServer:
    def __init__(self):
        self.server = Server("nexus-assistant")
        self._register_tools()

    def _register_tools(self):
        register_task_tools(self.server, SessionLocal)
        register_employee_tools(self.server, SessionLocal)
        register_run_tools(self.server, SessionLocal)
        register_knowledge_tools(self.server, SessionLocal)
        register_operation_tools(self.server, SessionLocal)

    async def run(self):
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options()
            )

async def start_mcp_server(db_session=None):
    server = AssistantMCPServer()
    await server.run()

if __name__ == "__main__":
    asyncio.run(start_mcp_server())
