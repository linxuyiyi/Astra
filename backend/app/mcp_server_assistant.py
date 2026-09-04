import asyncio

from app.assistant.mcp.server import start_mcp_server
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db_session:
        await start_mcp_server(db_session)

if __name__ == "__main__":
    asyncio.run(main())
