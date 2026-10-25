"""仅用于真实 DSH P0 的 MCP 工具，不接入产品工具目录。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="Nexus DSH compatibility probe", version="1")
proof = Path(sys.argv[1])


def record(kind: str, nonce: str) -> None:
    # 自有 fixture 的有限 nonce，禁止把任意模型输出写入报告。
    if not nonce.isalnum() or len(nonce) > 80:
        raise ValueError("invalid nonce")
    with proof.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": kind, "nonce": nonce}) + "\n")


@server.tool()
async def nexus_probe_echo(nonce: str) -> str:
    """Return the probe nonce and record real invocation. Use only when asked by the probe."""
    record("echo", nonce)
    return nonce


@server.tool()
async def nexus_probe_delay(nonce: str) -> str:
    """A bounded 30-second delay for testing cancellation. Do not repeat after cancellation."""
    record("delay_started", nonce)
    await asyncio.sleep(30)
    record("delay_finished", nonce)
    return nonce


if __name__ == "__main__":
    server.run(transport="stdio")
