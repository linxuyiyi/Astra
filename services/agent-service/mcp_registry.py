"""Nexus MCP 库登记与一次性连接测试，使用 AgentScope 2.0.10 官方 Storage/Workspace。"""

import re

from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

BUILTIN_NAMES = {"nexus-task", "nexus-schedule"}
MANAGED_NAME = re.compile(r"nexus_[a-z0-9_]+_[0-9a-f]{8}")


def register_mcp_registry(app, owner_id: str) -> None:
    from agentscope.app.storage import MCPRecord
    from agentscope.mcp import MCPClient

    def require_owner(request: Request) -> None:
        if request.headers.get("X-User-ID") != owner_id:
            raise HTTPException(403, "MCP 库登记仅供平台使用")

    def parse_client(data):
        try:
            return MCPClient.model_validate(data)
        except ValidationError:
            raise HTTPException(422, "MCP 连接配置无效") from None

    @app.put("/nexus/mcp-registry/{name}")
    async def register(name: str, request: Request):
        require_owner(request)
        if name not in BUILTIN_NAMES and not MANAGED_NAME.fullmatch(name):
            raise HTTPException(422, "MCP 运行名无效")
        body = await request.json()
        client = parse_client({**body["client"], "name": name})
        storage = app.state.storage
        async with app.state.message_bus.acquire_lock(f"nexus:mcp-registry:{owner_id}:{name}", ttl_secs=15):
            record = await storage.get_mcp_by_name(owner_id, name)
            if record is None:
                record = MCPRecord(user_id=owner_id, client=client)
            if record.hub_id:
                raise HTTPException(409, "MCP 运行名已被 AgentScope 市场资源使用")
            revision = body.get("revision")
            # 并发编辑中，较晚抵达的旧配置不能覆盖已经登记的新配置。
            if revision is None or not record.version or int(record.version) <= revision:
                record.client = client
                record.display_name = body.get("display_name")
                record.description = body.get("description", "")
                record.enabled = body.get("enabled", True)
                record.version = str(revision) if revision is not None else None
                await storage.upsert_mcp(owner_id, record)
        return {"id": record.id, "name": record.name, "version": record.version}

    @app.delete("/nexus/mcp-registry/{name}", status_code=204)
    async def unregister(name: str, request: Request):
        require_owner(request)
        if not MANAGED_NAME.fullmatch(name):
            raise HTTPException(422, "MCP 运行名无效")
        storage = app.state.storage
        async with app.state.message_bus.acquire_lock(f"nexus:mcp-registry:{owner_id}:{name}", ttl_secs=15):
            record = await storage.get_mcp_by_name(owner_id, name)
            if record is not None:
                if record.hub_id:
                    raise HTTPException(409, "该 MCP 由 AgentScope 市场管理")
                await storage.delete_mcp(owner_id, record.id)
        return Response(status_code=204)

    @app.post("/nexus/mcp-probe", status_code=201)
    async def mount_probe(request: Request, agent_id: str, session_id: str):
        require_owner(request)
        client = parse_client(await request.json())
        if not client.name.startswith("nexus_probe_"):
            raise HTTPException(422, "需要临时 MCP 运行名")
        workspace = await app.state.workspace_service.resolve(owner_id, agent_id, session_id)
        try:
            await workspace.add_mcp(client, agent_id=agent_id, session_id=session_id)
        except ValueError:
            raise HTTPException(409, "无法挂载测试 MCP") from None
        return Response(status_code=201)
