"""AgentScope 2.0.10 的 HTTP 客户端。不导入 agentscope 包。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

PINNED_VERSION = "2.0.10"
# Linux 上 LocalWorkspace.list_tools 的名称，加上本仓库启动脚本注册的 contact_employee。
# AgentScope 的 get_toolkit 还会自行挂上任务和团队工具，那些不在这份可选目录里。
LAUNCHER_TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "Write", "contact_employee", "contact_human")


class AgentScopeError(Exception):
    def __init__(self, detail: str, status_code: int = 503) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class AgentScopeClient:
    def __init__(
        self,
        base_url: str | None,
        user_id: str,
        http: httpx.AsyncClient | None = None,
        chat_model_config: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.user_id = user_id
        self.chat_model_config = chat_model_config
        self._http = http
        self._owns_http = http is None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    async def aclose(self) -> None:
        if self._http is not None and self._owns_http:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if not self.base_url:
            raise AgentScopeError("AgentScope 未连接，没有生成回复")
        if self._http is None:
            self._http = httpx.AsyncClient(base_url=self.base_url, timeout=20)
        return self._http

    def _headers(self) -> dict[str, str]:
        return {"X-User-ID": self.user_id}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        client = self._client()
        headers = dict(self._headers())
        headers.update(kwargs.pop("headers", {}))
        try:
            response = await client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AgentScopeError("AgentScope 未连接，没有生成回复") from exc
        if response.status_code >= 400:
            detail = response.text.strip() or response.reason_phrase
            raise AgentScopeError(f"AgentScope 返回 {response.status_code}：{detail}", response.status_code)
        return response

    async def probe(self) -> bool:
        if not self.configured:
            return False
        try:
            await self.request("GET", "/health")
        except AgentScopeError:
            return False
        return True

    async def create_agent(self, name: str, system_prompt: str, invite_description: str = "") -> str:
        response = await self.request(
            "POST",
            "/agent/",
            json={
                "name": name,
                "system_prompt": system_prompt,
                "chat_config": _invite_config(invite_description or name),
            },
        )
        payload = response.json()
        agent_id = payload.get("agent_id")
        if not agent_id:
            raise AgentScopeError("AgentScope 创建员工后没有返回 agent_id")
        return str(agent_id)

    async def update_agent(
        self,
        agent_id: str,
        name: str,
        system_prompt: str,
        invite_description: str = "",
    ) -> None:
        await self.request(
            "PATCH",
            f"/agent/{agent_id}",
            json={
                "name": name,
                "system_prompt": system_prompt,
                "chat_config": _invite_config(invite_description or name),
            },
        )

    async def delete_agent(self, agent_id: str) -> None:
        await self.request("DELETE", f"/agent/{agent_id}")

    async def ensure_gateway_credential(self, api_key: str, base_url: str) -> str:
        """登记指向网关 /v1 的 credential。密钥只出现在这次请求体里。"""

        payload = {"type": "openai_credential", "api_key": api_key, "base_url": base_url}
        try:
            listed = await self.request("GET", "/credential/")
            for item in listed.json().get("credentials", []):
                data = item.get("data") or {}
                same_gateway = (
                    data.get("type") == "openai_credential" and (data.get("base_url") or "") == base_url
                )
                if same_gateway and item.get("id"):
                    if (data.get("api_key") or "") == api_key:
                        return str(item["id"])
                    await self.request("PATCH", f"/credential/{item['id']}", json={"data": payload})
                    return str(item["id"])
            created = await self.request("POST", "/credential/", json={"data": payload})
        except AgentScopeError as exc:
            if api_key and api_key in exc.detail:
                raise AgentScopeError(exc.detail.replace(api_key, "[redacted]"), exc.status_code) from exc
            raise
        credential_id = created.json().get("credential_id")
        if not credential_id:
            raise AgentScopeError("AgentScope 没有返回 credential_id")
        return str(credential_id)

    async def replace_session_model(
        self,
        agent_id: str,
        session_id: str,
        chat_model_config: dict[str, Any],
    ) -> None:
        await self.request(
            "PATCH",
            f"/sessions/{session_id}",
            params={"agent_id": agent_id},
            json={"chat_model_config": chat_model_config},
        )

    async def replace_session_knowledge(
        self,
        agent_id: str,
        session_id: str,
        knowledge_base_ids: list[str],
    ) -> None:
        await self.request(
            "PATCH",
            f"/sessions/{session_id}",
            params={"agent_id": agent_id},
            json={"knowledge_config": {"knowledge_base_ids": knowledge_base_ids}},
        )

    async def create_session(
        self,
        agent_id: str,
        name: str,
        workspace_id: str | None = None,
        *,
        chat_model_config: dict[str, Any] | None = None,
        knowledge_base_ids: list[str] | None = None,
    ) -> str:
        body: dict[str, Any] = {"agent_id": agent_id, "name": name}
        if workspace_id:
            body["workspace_id"] = workspace_id
        # 空路由不能创建 Session，也不能用部署配置里的厂商模型补上。
        if not chat_model_config:
            if not self.configured:
                raise AgentScopeError("AgentScope 未连接，没有生成回复")
            raise AgentScopeError("请先选择模型路由", 422)
        body["chat_model_config"] = chat_model_config
        ids = [item for item in knowledge_base_ids or [] if item]
        if ids:
            body["knowledge_config"] = {"knowledge_base_ids": ids}
        response = await self.request(
            "POST",
            "/sessions/",
            json=body,
        )
        payload = response.json()
        session_id = payload.get("session_id")
        if not session_id:
            raise AgentScopeError("AgentScope 创建会话后没有返回 session_id")
        return str(session_id)

    async def set_permission_mode(self, agent_id: str, session_id: str, mode: str) -> None:
        """把 Session 设成指定权限模式。运行中的 Session 会拒绝修改，调用方可以忽略。"""
        if not self.configured or not agent_id or not session_id:
            return
        try:
            await self.request(
                "PATCH",
                f"/sessions/{session_id}",
                params={"agent_id": agent_id},
                json={"permission_mode": mode},
            )
        except AgentScopeError:
            return

    async def trigger_chat(
        self,
        agent_id: str,
        session_id: str,
        content: list[dict[str, Any]],
        user_name: str,
        verified_uid: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        display_text: str | None = None,
    ) -> str:
        message: dict[str, Any] = {"name": user_name, "role": "user", "content": content}
        metadata: dict[str, Any] = {}
        if verified_uid:
            metadata["verified_uid"] = verified_uid
        if attachments:
            metadata["attachments"] = attachments
        if display_text is not None:
            metadata["display_text"] = display_text
        if metadata:
            message["metadata"] = metadata
        response = await self.request(
            "POST",
            "/chat/",
            json={
                "agent_id": agent_id,
                "session_id": session_id,
                "input": message,
            },
        )
        payload = response.json()
        if payload.get("status") != "started":
            raise AgentScopeError("AgentScope 没有接受这次运行")
        return str(payload.get("session_id") or session_id)

    async def trigger_inputs(self, agent_id: str, session_id: str, messages: list[dict[str, Any]]) -> str:
        """把多条原消息一次送进会话。调用方保留消息 id，这里不新造一条。"""

        response = await self.request(
            "POST",
            "/chat/",
            json={"agent_id": agent_id, "session_id": session_id, "input": messages},
        )
        payload = response.json()
        if payload.get("status") != "started":
            raise AgentScopeError("AgentScope 没有接受这次运行")
        return str(payload.get("session_id") or session_id)

    async def record_message(
        self,
        session_id: str,
        message_id: str,
        name: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.request(
            "POST",
            "/nexus/record-message",
            json={
                "session_id": session_id,
                "message_id": message_id,
                "name": name,
                "text": text,
                "user_id": self.user_id,
                "metadata": metadata or {},
            },
        )

    async def list_user_mcp(self) -> list[dict[str, str]]:
        response = await self.request("GET", "/mcp")
        payload = response.json()
        if not isinstance(payload, list):
            raise AgentScopeError("AgentScope MCP 返回的不是列表")
        return _named_items(payload, "id", "name")

    async def list_workspace_mcp(self, agent_id: str, session_id: str) -> list[dict[str, Any]]:
        response = await self.request(
            "GET",
            "/workspace/mcp",
            params={"agent_id": agent_id, "session_id": session_id},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise AgentScopeError("AgentScope 工作区 MCP 返回的不是列表")
        return [item for item in payload if isinstance(item, dict)]

    async def list_skills(self) -> list[dict[str, str]]:
        response = await self.request("GET", "/skill")
        payload = response.json()
        if not isinstance(payload, list):
            raise AgentScopeError("AgentScope 技能库返回的不是列表")
        return _named_items(payload, "id", "skill_id", "name")

    async def list_knowledge_bases(self) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        page = 1
        while page <= 20:
            response = await self.request(
                "GET",
                "/knowledge_bases/",
                params={"page": page, "page_size": 128},
            )
            payload = response.json()
            if not isinstance(payload, dict):
                raise AgentScopeError("AgentScope 知识库返回的不是对象")
            rows = payload.get("knowledge_bases")
            if not isinstance(rows, list):
                raise AgentScopeError("AgentScope 知识库返回的不是列表")
            items.extend(_named_items(rows, "id"))
            total = payload.get("total")
            if not isinstance(total, int) or page * 128 >= total or not rows:
                break
            page += 1
        return items

    async def latest_event_cursor(self, agent_id: str, session_id: str) -> str | None:
        """上一轮已经落盘的重放游标。当前运行的事件在它之后。"""
        if not self.configured:
            return None
        try:
            response = await self.request(
                "GET",
                f"/sessions/{session_id}/messages",
                params={"agent_id": agent_id, "limit": 1},
            )
        except AgentScopeError:
            return None
        payload = response.json()
        cursor = payload.get("event_cursor") if isinstance(payload, dict) else None
        if isinstance(cursor, str) and cursor:
            return cursor
        return None

    async def read_session(self, agent_id: str, session_id: str) -> tuple[list[dict[str, Any]], str]:
        """同一页里取出消息和重放游标，避免历史和流各读一次中间漏事件。"""

        messages, cursor, _running = await self.read_session_detail(agent_id, session_id)
        return messages, cursor

    async def read_session_detail(
        self,
        agent_id: str,
        session_id: str,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """历史、游标和运行态来自同一次上游读取，不事后推断。"""

        collected: list[dict[str, Any]] = []
        event_cursor = ""
        is_running = False
        before: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"agent_id": agent_id, "limit": 200}
            if before:
                params["before"] = before
            response = await self.request("GET", f"/sessions/{session_id}/messages", params=params)
            payload = response.json()
            if not isinstance(payload, dict):
                raise AgentScopeError("AgentScope 会话消息返回的不是对象")
            rows = payload.get("messages")
            if not isinstance(rows, list):
                raise AgentScopeError("AgentScope 会话消息返回的不是列表")
            page = [item for item in rows if isinstance(item, dict)]
            if before is None:
                cursor = payload.get("event_cursor")
                event_cursor = cursor if isinstance(cursor, str) else ""
            collected = page + collected if before else page
            if not payload.get("has_more") or not page:
                break
            older = page[0].get("id")
            if not isinstance(older, str) or older == before:
                break
            before = older
        return collected, event_cursor, is_running

    async def interrupt_session(self, agent_id: str, session_id: str) -> None:
        """请求中断该会话正在跑的回复。空闲会话在上游是静默 no-op。"""

        await self.request(
            "POST",
            f"/sessions/{session_id}/interrupt",
            params={"agent_id": agent_id},
        )

    async def list_agent_sessions(self, agent_id: str) -> list[dict[str, Any]]:
        response = await self.request("GET", "/sessions/", params={"agent_id": agent_id})
        payload = response.json()
        if isinstance(payload, dict):
            items = payload.get("sessions") or []
        elif isinstance(payload, list):
            items = payload
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    async def list_session_messages(self, agent_id: str, session_id: str) -> list[dict[str, Any]]:
        messages, _cursor = await self.read_session(agent_id, session_id)
        return messages

    async def upload_workspace_skill(self, agent_id: str, session_id: str, directory: Path) -> None:
        skill = directory / "SKILL.md"
        data = skill.read_bytes()
        manifest = json.dumps(
            {"entries": [{"path": f"{directory.name}/SKILL.md", "size": len(data)}]},
            ensure_ascii=False,
        )
        await self.request(
            "POST",
            "/workspace/skill/upload",
            params={"agent_id": agent_id, "session_id": session_id},
            data={"manifest": manifest},
            files={"files": ("SKILL.md", data, "text/markdown")},
        )

    async def add_workspace_mcp(
        self,
        agent_id: str,
        session_id: str,
        *,
        url: str,
        headers: dict[str, str],
    ) -> None:
        payload = {
            "name": "nexus-task",
            "is_stateful": False,
            "mcp_config": {"type": "http_mcp", "url": url, "headers": headers, "timeout": 30},
        }
        try:
            await self.request(
                "POST",
                "/workspace/mcp",
                params={"agent_id": agent_id, "session_id": session_id},
                json=payload,
            )
            return
        except AgentScopeError as exc:
            if exc.status_code != 409:
                raise
        if await self._workspace_mcp_matches(agent_id, session_id, url=url, headers=headers):
            return
        await self.remove_workspace_mcp(agent_id, session_id)
        await self.request(
            "POST",
            "/workspace/mcp",
            params={"agent_id": agent_id, "session_id": session_id},
            json=payload,
        )

    async def _workspace_mcp_matches(
        self,
        agent_id: str,
        session_id: str,
        *,
        url: str,
        headers: dict[str, str],
    ) -> bool:
        try:
            response = await self.request(
                "GET",
                "/workspace/mcp",
                params={"agent_id": agent_id, "session_id": session_id},
            )
        except AgentScopeError:
            return False
        payload = response.json()
        if not isinstance(payload, list):
            return False
        for item in payload:
            if not isinstance(item, dict) or item.get("name") != "nexus-task":
                continue
            config = item.get("mcp_config") if isinstance(item.get("mcp_config"), dict) else {}
            current = config.get("headers") if isinstance(config.get("headers"), dict) else {}
            return config.get("url") == url and dict(current) == dict(headers)
        return False

    async def list_workspace_skills(self, agent_id: str, session_id: str) -> list[dict[str, Any]]:
        response = await self.request(
            "GET",
            "/workspace/skill",
            params={"agent_id": agent_id, "session_id": session_id},
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise AgentScopeError("AgentScope 技能列表不是数组")
        return [item for item in payload if isinstance(item, dict)]

    async def remove_workspace_mcp(self, agent_id: str, session_id: str, name: str = "nexus-task") -> None:
        try:
            await self.request(
                "DELETE",
                f"/workspace/mcp/{name}",
                params={"agent_id": agent_id, "session_id": session_id},
            )
        except AgentScopeError as exc:
            if exc.status_code != 404:
                raise

    async def remove_workspace_skill(self, agent_id: str, session_id: str, name: str) -> None:
        try:
            await self.request(
                "DELETE",
                f"/workspace/skill/{name}",
                params={"agent_id": agent_id, "session_id": session_id},
            )
        except AgentScopeError as exc:
            if exc.status_code != 404:
                raise

    async def list_launcher_tools(self) -> list[dict[str, str]]:
        if not await self.probe():
            raise AgentScopeError("AgentScope 未连接")
        return [{"id": name, "name": name} for name in LAUNCHER_TOOLS]

    def stream_session(self, agent_id: str, session_id: str, after: str | None = None) -> Any:
        client = self._client()
        params = {"agent_id": agent_id}
        if after:
            params["after"] = after
        return client.stream(
            "GET",
            f"/sessions/{session_id}/stream",
            params=params,
            headers=self._headers(),
            timeout=None,
        )


def _invite_config(description: str) -> dict[str, Any]:
    """员工 Agent 的对话配置。压缩用默认阈值，邀请说明给 AgentInvite。"""

    return {
        "context_config": {"trigger_ratio": 0.8, "reserve_ratio": 0.1},
        "invite_config": {
            "invitable": True,
            "invite_description": description.strip() or "同事",
        },
    }


def _named_items(rows: list[Any], *keys: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        item_id = ""
        for key in keys:
            if item.get(key):
                item_id = str(item[key])
                break
        if not item_id:
            continue
        items.append({"id": item_id, "name": str(item.get("name") or item_id)})
    return items
