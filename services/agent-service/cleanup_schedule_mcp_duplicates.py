"""一次性清理日程身份下的旧 MCP 副本。默认只列出候选。"""

import argparse
import asyncio
import os

from agentscope_sql import check_schema, create_storage, database_url
from mcp_registry import MANAGED_NAME
from schedule_resources import is_schedule_user
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine


async def cleanup(names: list[str], *, apply: bool) -> None:
    await check_schema()
    owner = os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")
    engine = create_async_engine(database_url())
    query = text("SELECT id, user_id, name FROM mcps WHERE name IN :names").bindparams(
        bindparam("names", expanding=True)
    )
    try:
        async with engine.connect() as connection:
            rows = (await connection.execute(query, {"names": names})).mappings().all()
    finally:
        await engine.dispose()
    async with create_storage() as storage:
        for row in rows:
            if not is_schedule_user(row["user_id"]):
                continue
            canonical = await storage.get_mcp_by_name(owner, row["name"])
            if not canonical or canonical.hub_id or not canonical.version:
                print(f"跳过 {row['name']}：请先在 Nexus 同步平台登记")
                continue
            if apply:
                await storage.delete_mcp(row["user_id"], row["id"])
            print(f"{'已删除' if apply else '待删除'} {row['name']} {row['user_id']} {row['id']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="删除已登记到平台库的日程副本")
    parser.add_argument("names", nargs="+", help="要处理的 Nexus MCP 稳定运行名")
    args = parser.parse_args()
    if any(not MANAGED_NAME.fullmatch(name) for name in args.names):
        parser.error("仅接受 Nexus 自建 MCP 的稳定运行名")
    asyncio.run(cleanup(args.names, apply=args.apply))
