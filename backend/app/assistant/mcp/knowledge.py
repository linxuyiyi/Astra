from app.assistant.tools import knowledge_tools
from mcp.server.mcpserver import MCPServer as Server


def register_knowledge_tools(server: Server, SessionLocal):
    @server.tool(name="nexus_knowledge_search", structured_output=True)
    async def nexus_knowledge_search(query: str):
        async with SessionLocal() as db_session:
            return await knowledge_tools.nexus_knowledge_search({"query": query}, "admin", db_session)
