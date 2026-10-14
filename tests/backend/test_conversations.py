# -*- coding: utf-8 -*-
"""Conversation 共同 Session factory 与 owner 绑定测试（任务 3.2.2）。

真实官方 ASGI + SQLite + 公开 SkillHub fixture + 真实本地 MCP 进程：
验证员工默认关联真实生效、Session 与 Agent 顶层 id 一致、能力快照、owner
隔离与幂等；以及失效/失败关联不得返回 ready（单元标记 mock）。
"""
from __future__ import annotations

import asyncio
import sys
import uuid

import httpx
import pytest
from fastapi import HTTPException

from fixtures import (
    FIXTURE_SKILL_CARD_ID,
    FIXTURE_SKILL_HUB_ID,
    FIXTURE_SKILL_NAME,
    FixtureSkillHub,
)
from mcp_fixture_http import (
    HTTP_MCP_NAME,
    SELECTED_TOOL,
    http_mcp_declaration,
    running_http_mcp,
)
from _stack import INTERNAL_TOKEN, real_stack

OWNER = "11111111-2222-3333-4444-555555555555"


def _valid_payload(**overrides) -> dict:
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "responsibilities": "负责自动化脚本开发与维护。",
        "instructions": "收到消息后先判断能否直接回答。",
        "avatar": "👩🏻‍💻",
    }
    payload.update(overrides)
    return payload


def _owner_headers(owner: str = OWNER) -> dict:
    return {"X-Nexus-Owner": owner}


async def _prepare_skill(adapter) -> str:
    """在 library 中准备真实 Skill（hub fixture）。"""
    skill = await adapter.install_skill_from_hub(
        FIXTURE_SKILL_HUB_ID,
        FIXTURE_SKILL_CARD_ID,
    )
    return skill["id"]


async def _install_http_mcp(adapter, url: str) -> str:
    """用官方 POST /workspace/mcp 安装真实无状态 Streamable HTTP MCP 到 library。"""
    throwaway_agent = await adapter.create_agent(
        name="mcp-installer",
        system_prompt="installer",
    )
    throwaway_session = await adapter.create_session(
        agent_id=throwaway_agent,
        name="mcp-installer",
    )
    declaration = http_mcp_declaration(url)
    declaration["enable_tools"] = [SELECTED_TOOL]
    await adapter.install_mcp(throwaway_agent, throwaway_session, declaration)
    mcps = await adapter.list_mcps()
    return next(m["id"] for m in mcps if m["name"] == HTTP_MCP_NAME)


async def _install_stdio_mcp(adapter) -> str:
    """安装一个 stdio MCP（当前版本明确不支持），返回 library id。"""
    throwaway_agent = await adapter.create_agent(
        name="stdio-installer",
        system_prompt="installer",
    )
    throwaway_session = await adapter.create_session(
        agent_id=throwaway_agent,
        name="stdio-installer",
    )
    from fixtures import MCP_FIXTURE_NAME, MCP_FIXTURE_SCRIPT

    await adapter.install_mcp(
        throwaway_agent,
        throwaway_session,
        {
            "name": MCP_FIXTURE_NAME,
            "is_stateful": True,
            "mcp_config": {
                "type": "stdio_mcp",
                "command": sys.executable,
                "args": [str(MCP_FIXTURE_SCRIPT)],
            },
        },
    )
    mcps = await adapter.list_mcps()
    return next(m["id"] for m in mcps if m["name"] == MCP_FIXTURE_NAME)


@pytest.mark.asyncio
async def test_conversation_binds_session_and_skill_capability(tmp_path):
    async with real_stack(
        tmp_path,
        skill_hubs=[FixtureSkillHub()],
        model_key="test-model-key",
    ) as (backend, adapter, client, upstream):
        skill_id = await _prepare_skill(adapter)
        employee = (
            await client.post(
                "/api/employees",
                json=_valid_payload(skills=[skill_id]),
            )
        ).json()
        assert employee["skills"] == [skill_id]

        response = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={
                "employee_id": employee["id"],
                "title": "首个对话",
                "client_key": "skill-conv-1",
            },
        )
        assert response.status_code == 201, response.text
        conv = response.json()
        assert conv["status"] == "ready"
        assert conv["session_id"]
        assert conv["agent_id"] == employee["agent_id"]
        assert conv["capability_snapshot"] == {
            "knowledge": [],
            "skills": [skill_id],
            "tools": [],
        }

        # 原生 Session 确实属于该员工 Agent，且能力真实装载在读回的 workspace。
        views = await adapter.list_sessions(employee["agent_id"])
        session = next(
            v["session"]
            for v in views["sessions"]
            if v["session"]["id"] == conv["session_id"]
        )
        assert session["agent_id"] == employee["agent_id"]
        skills = await adapter.list_workspace_skills(
            employee["agent_id"],
            conv["session_id"],
        )
        assert any(s["name"] == FIXTURE_SKILL_NAME for s in skills)


@pytest.mark.asyncio
async def test_conversation_binds_stateless_http_mcp(tmp_path):
    """真实无状态 Streamable HTTP MCP 经员工默认关联在 workspace 读回并健康。"""
    with running_http_mcp() as url:
        async with real_stack(
            tmp_path,
            model_key="test-model-key",
        ) as (backend, adapter, client, upstream):
            mcp_id = await _install_http_mcp(adapter, url)
            employee = (
                await client.post(
                    "/api/employees",
                    json=_valid_payload(tools=[mcp_id]),
                )
            ).json()
            assert employee["tools"] == [mcp_id]

            response = await client.post(
                "/api/conversations",
                headers=_owner_headers(),
                json={"employee_id": employee["id"], "client_key": "mcp-http-1"},
            )
            assert response.status_code == 201, response.text
            conv = response.json()
            assert conv["status"] == "ready"
            assert conv["capability_snapshot"]["tools"] == [mcp_id]
            mcps = await adapter.list_workspace_mcps(
                employee["agent_id"],
                conv["session_id"],
            )
            installed = next(m for m in mcps if m["name"] == HTTP_MCP_NAME)
            assert installed["is_healthy"] is True
            assert installed["is_stateful"] is False
            assert installed["mcp_config"]["type"] == "http_mcp"
            tool_names = {t["name"] for t in installed["tools"]}
            # enable_tools 精确选择：只有所选工具被装配，未选工具不出现。
            assert tool_names == {f"mcp__{HTTP_MCP_NAME}__{SELECTED_TOOL}"}

            # 运行授权必须返回所选 MCP 名称（客户端身份），供 service 精确筛选。
            authorized = await client.post(
                "/api/internal/authorize-run",
                headers={"X-Nexus-Internal-Token": INTERNAL_TOKEN},
                json={
                    "agent_id": employee["agent_id"],
                    "session_id": conv["session_id"],
                },
            )
            assert authorized.status_code == 200, authorized.text
            body = authorized.json()
            assert body["mcps"] == [HTTP_MCP_NAME]
            assert body["tools"] == [f"mcp__{HTTP_MCP_NAME}__{SELECTED_TOOL}"]


@pytest.mark.asyncio
async def test_stdio_mcp_is_projected_unsupported(tmp_path):
    """stdio MCP 安全投影为不可选，保存为员工工具明确失败（负向验收）。"""
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        mcp_id = await _install_stdio_mcp(adapter)
        catalog = (await client.get("/api/resources/mcp")).json()
        items = {item["id"]: item for item in catalog["mcps"]["items"]}
        projected = items[mcp_id]
        assert projected["selectable"] is False
        assert projected["reason"]
        # 安全投影不返回连接配置/环境变量。
        assert "mcp_config" not in projected
        assert "command" not in projected

        response = await client.post(
            "/api/employees",
            json=_valid_payload(tools=[mcp_id]),
        )
        assert response.status_code == 422, response.text
        assert "tools" in response.json()["detail"]["errors"]


@pytest.mark.asyncio
async def test_owner_required_and_isolated(tmp_path):
    async with real_stack(tmp_path, model_key="test-model-key") as (backend, adapter, client, upstream):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()

        missing = await client.post(
            "/api/conversations",
            json={"employee_id": employee["id"]},
        )
        assert missing.status_code == 400
        bad = await client.post(
            "/api/conversations",
            headers={"X-Nexus-Owner": "not-a-uuid"},
            json={"employee_id": employee["id"]},
        )
        assert bad.status_code == 400

        owner_a = str(uuid.uuid4())
        owner_b = str(uuid.uuid4())
        conv_a = (
            await client.post(
                "/api/conversations",
                headers=_owner_headers(owner_a),
                json={"employee_id": employee["id"], "client_key": "iso-key"},
            )
        ).json()
        conv_b = (
            await client.post(
                "/api/conversations",
                headers=_owner_headers(owner_b),
                json={"employee_id": employee["id"], "client_key": "iso-key"},
            )
        ).json()
        # 不同 owner 得到不同 Conversation 与不同原生 Session。
        assert conv_a["id"] != conv_b["id"]
        assert conv_a["session_id"] != conv_b["session_id"]
        assert conv_a["owner"] == owner_a
        assert conv_b["owner"] == owner_b

        # 同一员工两个会话的 workspace 物理隔离（PER_SESSION）。
        views = await adapter.list_sessions(employee["agent_id"])
        ws = {v["session"]["id"]: v["session"]["config"]["workspace_id"]
              for v in views["sessions"]}
        assert ws[conv_a["session_id"]] != ws[conv_b["session_id"]]


@pytest.mark.asyncio
async def test_client_key_idempotent_no_duplicate_session(tmp_path):
    async with real_stack(tmp_path, model_key="test-model-key") as (backend, adapter, client, upstream):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        body = {"employee_id": employee["id"], "client_key": "key-1"}
        first = (
            await client.post(
                "/api/conversations",
                headers=_owner_headers(),
                json=body,
            )
        ).json()
        second = (
            await client.post(
                "/api/conversations",
                headers=_owner_headers(),
                json=body,
            )
        ).json()
        assert first["id"] == second["id"]
        assert first["session_id"] == second["session_id"]
        views = await adapter.list_sessions(employee["agent_id"])
        assert len(views["sessions"]) == 1


@pytest.mark.asyncio
async def test_disabled_employee_cannot_create_conversation(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        disabled = await client.patch(
            f"/api/employees/{employee['id']}",
            json={"enabled": False},
        )
        assert disabled.status_code == 200
        response = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"], "client_key": "disabled-1"},
        )
        assert response.status_code == 409
        assert "禁用" in response.json()["detail"]


@pytest.mark.asyncio
async def test_failed_capability_binding_not_ready(tmp_path):
    """能力装载 failed 时必须明确失败、不得返回 ready（单元标记 mock）。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Employee

    _MODEL = {
        "type": "deepseek_chat",
        "credential_id": "c1",
        "model": "deepseek-flash",
        "parameters": {},
    }

    class FakeAdapter:
        async def service_defaults(self):
            return {
                "guard_configured": True,
                "model_configured": True,
                "chat_model_config": dict(_MODEL),
            }

        async def list_sessions(self, agent_id):
            return {
                "sessions": [
                    {
                        "session": {
                            "id": "sess-1",
                            "agent_id": agent_id,
                            "config": {
                                "name": "nexus-conv-test",
                                "workspace_id": "ws-1",
                                "chat_model_config": dict(_MODEL),
                                "knowledge_config": {},
                            },
                        },
                    },
                ],
                "total": 1,
            }

        async def list_skills(self):
            return [{"id": "s1", "name": "broken-skill", "enabled": True}]

        async def add_skills_from_library(self, agent_id, session_id, ids):
            return {"added": [], "failed": {"s1": "boom"}}

        async def list_workspace_skills(self, agent_id, session_id):
            return []

    employee = Employee(
        id="emp_x",
        agent_id="agent-1",
        name="员工",
        normalized_name="员工",
        domain="领域",
        normalized_domain="领域",
        responsibilities="r",
        instructions="i",
        avatar="🤖",
        enabled=True,
        knowledge=[],
        skills=["s1"],
        tools=[],
    )
    target = await conv_mod._precheck(FakeAdapter(), employee)
    with pytest.raises(conv_mod.ConversationError) as excinfo:
        await conv_mod._verify_and_bind(
            FakeAdapter(),
            target,
            "sess-1",
            "nexus-conv-test",
        )
    assert excinfo.value.status_code == 502


@pytest.mark.asyncio
async def test_session_agent_mismatch_fails(tmp_path):
    """原生返回的 Session 不属于该 Agent 时明确失败，不返回 ready。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Employee

    class FakeAdapter:
        async def service_defaults(self):
            return {
                "guard_configured": True,
                "model_configured": True,
                "chat_model_config": {"type": "deepseek_chat", "model": "m"},
            }

        async def list_sessions(self, agent_id):
            return {
                "sessions": [
                    {
                        "session": {
                            "id": "sess-1",
                            "agent_id": "some-other-agent",
                            "config": {},
                        },
                    },
                ],
                "total": 1,
            }

    employee = Employee(
        id="emp_x",
        agent_id="agent-1",
        name="员工",
        normalized_name="员工",
        domain="领域",
        normalized_domain="领域",
        responsibilities="r",
        instructions="i",
        avatar="🤖",
        enabled=True,
        knowledge=[],
        skills=[],
        tools=[],
    )
    target = await conv_mod._precheck(FakeAdapter(), employee)
    with pytest.raises(conv_mod.ConversationError):
        await conv_mod._verify_and_bind(
            FakeAdapter(),
            target,
            "sess-1",
            "nexus-conv-test",
        )


@pytest.mark.asyncio
async def test_verify_mcp_capability_unit(tmp_path):
    """from-library failed / 未健康读回都必须明确失败（单元标记 mock）。"""
    from nexus_backend import conversations as conv_mod

    _MCP_CONFIG = {"type": "http_mcp", "url": "http://127.0.0.1:9/mcp"}

    class FakeAdapter:
        def __init__(self, *, failed=None, healthy=True, malformed=False):
            self._failed = failed or {}
            self._healthy = healthy
            self._malformed = malformed

        async def list_mcps(self):
            return [
                {
                    "id": "m1",
                    "name": "nexus-compute",
                    "enabled": True,
                },
            ]

        async def mcp_support_map(self):
            return [{"id": "m1", "selectable": True, "reason": None}]

        async def add_mcps_from_library(self, agent_id, session_id, ids):
            if self._malformed:
                return "not-a-dict"
            return {
                "added": [] if self._failed else ["nexus-compute"],
                "failed": self._failed,
            }

        async def list_workspace_mcps(self, agent_id, session_id):
            return [
                {
                    "name": "nexus-compute",
                    "is_stateful": False,
                    "mcp_config": _MCP_CONFIG,
                    "is_healthy": self._healthy,
                    "tools": [{"name": "mcp__nexus-compute__compute"}],
                },
            ]

    await conv_mod._verify_workspace_capability(
        FakeAdapter(),
        agent_id="a",
        session_id="s",
        kind="tools",
        ids=["m1"],
    )
    with pytest.raises(conv_mod.ConversationError):
        await conv_mod._verify_workspace_capability(
            FakeAdapter(failed={"m1": "boom"}),
            agent_id="a",
            session_id="s",
            kind="tools",
            ids=["m1"],
        )
    with pytest.raises(conv_mod.ConversationError):
        await conv_mod._verify_workspace_capability(
            FakeAdapter(healthy=False),
            agent_id="a",
            session_id="s",
            kind="tools",
            ids=["m1"],
        )
    # from-library 响应畸形（非 added/failed 结构）不得假 ready。
    with pytest.raises(conv_mod.ConversationError):
        await conv_mod._verify_workspace_capability(
            FakeAdapter(malformed=True),
            agent_id="a",
            session_id="s",
            kind="tools",
            ids=["m1"],
        )


@pytest.mark.asyncio
async def test_idempotent_ready_returns_200_and_no_new_session(tmp_path):
    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        body = {"employee_id": employee["id"], "client_key": "k-1"}
        first = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert first.status_code == 201
        second = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]
        views = await adapter.list_sessions(employee["agent_id"])
        assert len(views["sessions"]) == 1


@pytest.mark.asyncio
async def test_idempotent_key_other_employee_is_409(tmp_path):
    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee_a = (
            await client.post(
                "/api/employees",
                json=_valid_payload(name="甲", domain="领域甲"),
            )
        ).json()
        employee_b = (
            await client.post(
                "/api/employees",
                json=_valid_payload(name="乙", domain="领域乙"),
            )
        ).json()
        first = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee_a["id"], "client_key": "shared"},
        )
        assert first.status_code == 201
        conflict = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee_b["id"], "client_key": "shared"},
        )
        assert conflict.status_code == 409, conflict.text


@pytest.mark.asyncio
async def test_non_ready_idempotent_key_is_409_not_fake_201(tmp_path):
    """幂等键已存在但非 ready 且无法恢复时返回 409，不返回 201 假成功。"""
    from starlette.responses import Response

    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Conversation, Database, Employee

    db = Database(tmp_path / "conv.db")
    await db.init()
    async with db.session() as session:
        session.add(
            Employee(
                id="emp_x",
                agent_id="agent-1",
                name="员工",
                normalized_name="员工",
                domain="领域",
                normalized_domain="领域",
                responsibilities="r",
                instructions="i",
                avatar="🤖",
                enabled=True,
                knowledge=[],
                skills=[],
                tools=[],
                sync_state="ready",
            ),
        )
        session.add(
            Conversation(
                id="conv_x",
                owner=OWNER,
                employee_id="emp_x",
                agent_id="agent-1",
                session_id=None,
                session_marker="nexus-conv-conv_x",
                title="新对话",
                capability_snapshot={},
                create_intent={
                    "agent_id": "agent-1",
                    "chat_model_config": {"type": "deepseek_chat", "model": "m"},
                    "knowledge": [],
                    "skills": [],
                    "tools": [],
                    "generation_id": "gen-x",
                },
                client_key="k",
                status="creating",
            ),
        )
        await session.commit()
    existing = await conv_mod._reload_conversation(db, "conv_x")

    class FakeAdapter:
        async def list_sessions(self, agent_id):
            return {"sessions": [], "total": 0}

        async def extension_health(self):
            # 同代次：不能猜测旧创建请求已退出，必须 409。
            return {"generation_id": "gen-x"}

    response = Response()
    with pytest.raises(HTTPException) as excinfo:
        await conv_mod._resolve_existing_locked(
            db,
            FakeAdapter(),
            response,
            existing,
            employee_id="emp_x",
            title="新对话",
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.detail["conversation_id"] == "conv_x"
    await db.dispose()


@pytest.mark.asyncio
async def test_malformed_session_201_keeps_pending_not_fake_success(
    tmp_path,
    monkeypatch,
):
    """原生创建返回异常 2xx（缺 session_id）时保留未完成，不返回成功。"""
    from nexus_backend.adapter import UpstreamError

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()

        async def fake_create_session(**kwargs):
            raise UpstreamError("POST", "/sessions/", 201, "malformed")

        monkeypatch.setattr(adapter, "create_session", fake_create_session)
        response = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"], "client_key": "malformed-1"},
        )
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        assert detail["status"] == "creating"
        assert detail["conversation_id"]


@pytest.mark.asyncio
async def test_timeout_after_session_keeps_known_id_not_fake_ready(
    tmp_path,
    monkeypatch,
):
    """拿到 sessionID 后的能力核对超时：保留已知 ID，不假 ready。"""
    from nexus_backend.adapter import UpstreamError

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        original_list = adapter.list_sessions
        state = {"first": True}

        async def flaky_list(agent_id):
            if state["first"]:
                state["first"] = False
                raise UpstreamError("GET", "/sessions/", None, "timeout")
            return await original_list(agent_id)

        monkeypatch.setattr(adapter, "list_sessions", flaky_list)
        response = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"], "client_key": "timeout-1"},
        )
        assert response.status_code == 504, response.text
        detail = response.json()["detail"]
        assert detail["status"] == "creating"
        assert detail["session_id"]


@pytest.mark.asyncio
async def test_recovery_reuses_marker_without_new_post(tmp_path, monkeypatch):
    """未完成会话按精确 session_marker 恢复为 ready，不再次 POST。"""
    from nexus_backend.db import Conversation

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        body = {"employee_id": employee["id"], "client_key": "rec-1"}
        created = (
            await client.post(
                "/api/conversations", headers=_owner_headers(), json=body
            )
        ).json()
        assert created["status"] == "ready"
        original_session = created["session_id"]

        # 模拟“已 POST 但未回填关联”：清空 session_id 与状态，保留精确标记与
        # 创建意图（真实未完成会话在 POST 前已持久化意图；ready 会清理它）。
        defaults = await adapter.service_defaults()
        async with backend.state.db.session() as session:
            row = await session.get(Conversation, created["id"])
            row.status = "creating"
            row.session_id = None
            row.create_intent = {
                "agent_id": employee["agent_id"],
                "chat_model_config": dict(defaults["chat_model_config"]),
                "knowledge": [],
                "skills": [],
                "tools": [],
                "generation_id": "recovery-test-gen",
            }
            await session.commit()

        calls = {"n": 0}
        original_create = adapter.create_session

        async def counting_create(**kwargs):
            calls["n"] += 1
            return await original_create(**kwargs)

        monkeypatch.setattr(adapter, "create_session", counting_create)
        recovered = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "ready"
        assert recovered.json()["session_id"] == original_session
        assert calls["n"] == 0


@pytest.mark.asyncio
async def test_client_key_required(tmp_path):
    """client_key 必填：缺失/空/超长明确 422，保证创建可重复定位。"""
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        missing = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"]},
        )
        assert missing.status_code == 422
        empty = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"], "client_key": "   "},
        )
        assert empty.status_code == 422
        too_long = await client.post(
            "/api/conversations",
            headers=_owner_headers(),
            json={"employee_id": employee["id"], "client_key": "x" * 200},
        )
        assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_concurrent_same_key_single_consistent_result(tmp_path, monkeypatch):
    """首次绑定仍在进行时同 key 重试：同一员工锁串行，结果一致且不删 ready。"""
    from nexus_backend import conversations as conv_mod

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        original = conv_mod._verify_and_bind
        state = {"first": True}
        entered = asyncio.Event()

        async def slow(adapter_, target, session_id, marker):
            if state["first"]:
                state["first"] = False
                entered.set()
                await asyncio.sleep(0.4)
            return await original(adapter_, target, session_id, marker)

        monkeypatch.setattr(conv_mod, "_verify_and_bind", slow)
        body = {"employee_id": employee["id"], "client_key": "concurrent-1"}
        first_task = asyncio.ensure_future(
            client.post("/api/conversations", headers=_owner_headers(), json=body),
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        second_task = asyncio.ensure_future(
            client.post("/api/conversations", headers=_owner_headers(), json=body),
        )
        r1, r2 = await asyncio.gather(first_task, second_task)
        assert {r1.status_code, r2.status_code} == {200, 201}, (r1.text, r2.text)
        a, b = r1.json(), r2.json()
        assert a["id"] == b["id"]
        assert a["session_id"] == b["session_id"]
        assert a["status"] == b["status"] == "ready"
        # ready 的 Session 未被任何清理路径删除，且只有一个原生 Session。
        views = await adapter.list_sessions(employee["agent_id"])
        assert [v["session"]["id"] for v in views["sessions"]] == [a["session_id"]]


@pytest.mark.asyncio
async def test_first_binding_failure_then_same_key_retry(tmp_path, monkeypatch):
    """首次能力绑定失败清理占位；同 key 重试重新创建为 ready。"""
    from nexus_backend import conversations as conv_mod

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        original = conv_mod._verify_and_bind
        calls = {"n": 0}

        async def flaky(adapter_, target, session_id, marker):
            calls["n"] += 1
            if calls["n"] == 1:
                raise conv_mod.ConversationError(502, "模拟首次绑定失败")
            return await original(adapter_, target, session_id, marker)

        monkeypatch.setattr(conv_mod, "_verify_and_bind", flaky)
        body = {"employee_id": employee["id"], "client_key": "retry-1"}
        first = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert first.status_code == 502, first.text
        second = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert second.status_code == 201, second.text
        assert second.json()["status"] == "ready"
        views = await adapter.list_sessions(employee["agent_id"])
        assert len(views["sessions"]) == 1


@pytest.mark.asyncio
async def test_unknown_create_retry_release_requires_generation_change(
    tmp_path,
    monkeypatch,
):
    """结果未知的创建：同代次不猜未创建（409）；代次变化且标记不存在才释放重试。"""
    from nexus_backend.adapter import UpstreamError
    from nexus_backend.db import Conversation

    async with real_stack(tmp_path, model_key="test-model-key") as (
        backend,
        adapter,
        client,
        upstream,
    ):
        employee = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        original = adapter.create_session

        async def lost(**kwargs):
            raise UpstreamError("POST", "/sessions/", None, "timeout")

        monkeypatch.setattr(adapter, "create_session", lost)
        body = {"employee_id": employee["id"], "client_key": "unknown-1"}
        first = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert first.status_code == 504, first.text
        conv_id = first.json()["detail"]["conversation_id"]

        # 同代次：不能猜测旧创建请求已退出，拒绝释放。
        same = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert same.status_code == 409, same.text

        # 模拟服务重启（代次变化）：占位记录的代次被改写为旧值。
        async with backend.state.db.session() as session:
            row = await session.get(Conversation, conv_id)
            intent = dict(row.create_intent)
            intent["generation_id"] = "old-generation"
            row.create_intent = intent
            await session.commit()

        monkeypatch.setattr(adapter, "create_session", original)
        third = await client.post(
            "/api/conversations", headers=_owner_headers(), json=body
        )
        assert third.status_code == 201, third.text
        assert third.json()["status"] == "ready"


@pytest.mark.asyncio
async def test_cleanup_confirm_missing_converges(tmp_path):
    """DELETE 响应丢失后确认原生不存在：收敛为已清理并清本地占位，不重建。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.adapter import UpstreamError
    from nexus_backend.db import Conversation, Database

    db = Database(tmp_path / "cleanup.db")
    await db.init()
    async with db.session() as session:
        session.add(
            Conversation(
                id="conv_clean",
                owner=OWNER,
                employee_id="emp_x",
                agent_id="agent-1",
                session_id="sess-1",
                session_marker="nexus-conv-conv_clean",
                title="新对话",
                capability_snapshot={},
                create_intent={
                    "agent_id": "agent-1",
                    "chat_model_config": {"type": "deepseek_chat", "model": "m"},
                    "knowledge": [],
                    "skills": [],
                    "tools": [],
                    "generation_id": "g",
                },
                client_key="k",
                status="creating",
            ),
        )
        await session.commit()

    class FakeAdapter:
        def __init__(self, results, *, name="nexus-conv-conv_clean"):
            self._results = list(results)
            self._name = name
            self.delete_calls = 0

        async def list_sessions(self, agent_id):
            return {
                "sessions": [
                    {
                        "session": {
                            "id": "sess-1",
                            "agent_id": "agent-1",
                            "config": {"name": self._name},
                        },
                    },
                ],
                "total": 1,
            }

        async def delete_session(
            self,
            agent_id,
            session_id,
            *,
            ignore_missing=False,
        ):
            self.delete_calls += 1
            result = self._results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    # 第一次 DELETE 实际成功但响应丢失 → 结果未知，保留未完成。
    outcome, row = await conv_mod._handle_failed_binding(
        db,
        FakeAdapter([UpstreamError("DELETE", "/sessions/sess-1", None, "lost")]),
        "conv_clean",
        "agent-1",
        session_id="sess-1",
        marker="nexus-conv-conv_clean",
    )
    assert outcome == "retained"
    assert row is not None and row.status == "incomplete"

    # 再次确认原生已不存在（404 语义）→ 收敛为已清理并删除本地占位。
    outcome, row = await conv_mod._handle_failed_binding(
        db,
        FakeAdapter([False]),
        "conv_clean",
        "agent-1",
        session_id="sess-1",
        marker="nexus-conv-conv_clean",
    )
    assert outcome == "confirmed"
    assert row is None
    assert await conv_mod._reload_conversation(db, "conv_clean") is None
    await db.dispose()


def _creating_row(*, session_id=None, marker="nexus-conv-conv_race", gen="old-gen"):
    from nexus_backend.db import Conversation

    return Conversation(
        id="conv_race",
        owner=OWNER,
        employee_id="emp_x",
        agent_id=None,
        session_id=session_id,
        session_marker=marker,
        title="新对话",
        capability_snapshot={},
        create_intent={
            "agent_id": "agent-1",
            "chat_model_config": {"type": "deepseek_chat", "model": "m"},
            "knowledge": [],
            "skills": [],
            "tools": [],
            "generation_id": gen,
        },
        client_key="k",
        status="creating",
    )


def _session_view(marker):
    return {
        "id": "sess-landed",
        "agent_id": "agent-1",
        "config": {
            "name": marker,
            "workspace_id": "ws-landed",
            "chat_model_config": {"type": "deepseek_chat", "model": "m"},
            "knowledge_config": {"knowledge_base_ids": [], "parameters": {}},
        },
    }


@pytest.mark.asyncio
async def test_release_rescans_marker_and_recovers_when_landed(tmp_path):
    """竞态：初扫 marker 缺失后旧 POST 落库；代次变化仍须重扫恢复，不释放。"""
    from starlette.responses import Response

    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Database

    db = Database(tmp_path / "race.db")
    await db.init()
    marker = "nexus-conv-conv_race"
    async with db.session() as session:
        session.add(_creating_row(marker=marker))
        await session.commit()
    existing = await conv_mod._reload_conversation(db, "conv_race")

    class FakeAdapter:
        def __init__(self):
            self.list_calls = 0

        async def extension_health(self):
            return {"generation_id": "new-gen"}

        async def list_sessions(self, agent_id):
            self.list_calls += 1
            if self.list_calls == 1:
                # 初次扫描：旧 POST 尚未落库。
                return {"sessions": [], "total": 0}
            # 重扫：旧 POST 已在本代次落库为既有记录。
            return {
                "sessions": [{"session": _session_view(marker)}],
                "total": 1,
            }

    response = Response()
    result = await conv_mod._resolve_existing_locked(
        db,
        FakeAdapter(),
        response,
        existing,
        employee_id="emp_x",
        title="新对话",
    )
    assert result is not None
    assert result["status"] == "ready"
    assert result["session_id"] == "sess-landed"
    assert await conv_mod._reload_conversation(db, "conv_race") is not None
    await db.dispose()


@pytest.mark.asyncio
async def test_release_blocked_when_generation_unstable_during_rescan(tmp_path):
    """重扫前后代次再次变化：保留占位，不释放、不重复创建。"""
    from starlette.responses import Response

    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Database

    db = Database(tmp_path / "race2.db")
    await db.init()
    async with db.session() as session:
        session.add(_creating_row())
        await session.commit()
    existing = await conv_mod._reload_conversation(db, "conv_race")

    class FakeAdapter:
        def __init__(self):
            self._gens = iter(["gen-2", "gen-3"])

        async def extension_health(self):
            return {"generation_id": next(self._gens)}

        async def list_sessions(self, agent_id):
            return {"sessions": [], "total": 0}

    response = Response()
    with pytest.raises(HTTPException) as excinfo:
        await conv_mod._resolve_existing_locked(
            db,
            FakeAdapter(),
            response,
            existing,
            employee_id="emp_x",
            title="新对话",
        )
    assert excinfo.value.status_code == 409
    assert await conv_mod._reload_conversation(db, "conv_race") is not None
    await db.dispose()


@pytest.mark.asyncio
async def test_known_absent_converges_same_generation(tmp_path):
    """已知 Session id 且原生确认缺失：同代次即收敛清占位，无须重启。"""
    from starlette.responses import Response

    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Database

    db = Database(tmp_path / "known.db")
    await db.init()
    async with db.session() as session:
        session.add(_creating_row(session_id="sess-gone"))
        await session.commit()
    existing = await conv_mod._reload_conversation(db, "conv_race")

    class FakeAdapter:
        async def extension_health(self):
            # 同名代次（记录 gen="old-gen"）：仍允许收敛，不要求重启。
            return {"generation_id": "old-gen"}

        async def list_sessions(self, agent_id):
            return {"sessions": [], "total": 0}

    response = Response()
    result = await conv_mod._resolve_existing_locked(
        db,
        FakeAdapter(),
        response,
        existing,
        employee_id="emp_x",
        title="新对话",
    )
    assert result is None  # 释放占位，调用方重新创建
    assert await conv_mod._reload_conversation(db, "conv_race") is None
    await db.dispose()


@pytest.mark.asyncio
async def test_cleanup_never_deletes_foreign_session(tmp_path):
    """原生返回的 Session 名称标记不符：绝不 DELETE，保留未完成并明确失败。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Conversation, Database

    db = Database(tmp_path / "foreign.db")
    await db.init()
    async with db.session() as session:
        session.add(_creating_row(session_id="sess-foreign"))
        await session.commit()

    class FakeAdapter:
        def __init__(self):
            self.delete_calls = 0

        async def list_sessions(self, agent_id):
            view = _session_view("nexus-conv-someone-else")
            view["id"] = "sess-foreign"
            return {"sessions": [{"session": view}], "total": 1}

        async def delete_session(self, agent_id, session_id, *, ignore_missing=False):
            self.delete_calls += 1
            return True

    adapter = FakeAdapter()
    outcome, row = await conv_mod._handle_failed_binding(
        db,
        adapter,
        "conv_race",
        "agent-1",
        session_id="sess-foreign",
        marker="nexus-conv-conv_race",
    )
    assert outcome == "foreign"
    assert adapter.delete_calls == 0
    assert row is not None and row.status == "incomplete"
    assert await conv_mod._reload_conversation(db, "conv_race") is not None
    await db.dispose()


@pytest.mark.asyncio
async def test_session_list_malformed_is_unknown_not_absent(tmp_path):
    """Session 列表契约畸形（顶层/条目/缺字段）一律 unknown，合法空数组才 absent。"""
    from nexus_backend import conversations as conv_mod

    class BadTop:
        async def list_sessions(self, agent_id):
            return {"sessions": "invalid"}

    class BadEntry:
        async def list_sessions(self, agent_id):
            return {"sessions": [{"session": "not-an-object"}]}

    class MissingFields:
        async def list_sessions(self, agent_id):
            return {"sessions": [{"session": {"id": "x"}}]}

    class MissingConfig:
        async def list_sessions(self, agent_id):
            return {
                "sessions": [
                    {"session": {"id": "x", "agent_id": "a"}},
                ],
            }

    class EmptyArray:
        async def list_sessions(self, agent_id):
            return {"sessions": [], "total": 0}

    for adapter in (BadTop(), BadEntry(), MissingFields(), MissingConfig()):
        assert (
            await conv_mod._session_owned_by(adapter, "a", "s", "m")
            == "unknown"
        )
    assert await conv_mod._session_owned_by(EmptyArray(), "a", "s", "m") == "absent"


@pytest.mark.asyncio
async def test_cleanup_malformed_list_does_not_delete(tmp_path):
    """畸形 Session 列表：清理返回 unknown、绝不 DELETE，占位保留 incomplete。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Database

    db = Database(tmp_path / "malformed.db")
    await db.init()
    async with db.session() as session:
        session.add(_creating_row())
        await session.commit()

    class FakeAdapter:
        def __init__(self):
            self.delete_calls = 0

        async def list_sessions(self, agent_id):
            return {"sessions": "invalid"}

        async def delete_session(self, agent_id, session_id, *, ignore_missing=False):
            self.delete_calls += 1
            return True

    adapter = FakeAdapter()
    assert (
        await conv_mod._cleanup_session(
            adapter,
            "agent-1",
            "sess-x",
            "nexus-conv-conv_race",
        )
        == "unknown"
    )
    outcome, row = await conv_mod._handle_failed_binding(
        db,
        adapter,
        "conv_race",
        "agent-1",
        session_id="sess-x",
        marker="nexus-conv-conv_race",
    )
    assert outcome == "retained"
    assert adapter.delete_calls == 0
    assert row is not None and row.status == "incomplete"
    assert await conv_mod._reload_conversation(db, "conv_race") is not None
    await db.dispose()


@pytest.mark.asyncio
async def test_recover_malformed_session_list_is_unavailable(tmp_path):
    """恢复时列表畸形：unavailable（保留占位、不返回 ready、不释放、不重 POST）。"""
    from starlette.responses import Response

    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Database

    db = Database(tmp_path / "malformed2.db")
    await db.init()
    async with db.session() as session:
        session.add(_creating_row())
        await session.commit()
    existing = await conv_mod._reload_conversation(db, "conv_race")

    class FakeAdapter:
        async def list_sessions(self, agent_id):
            return {"sessions": [{"session": "bad"}]}

        async def extension_health(self):
            return {"generation_id": "old-gen"}

    response = Response()
    with pytest.raises(HTTPException) as excinfo:
        await conv_mod._resolve_existing_locked(
            db,
            FakeAdapter(),
            response,
            existing,
            employee_id="emp_x",
            title="新对话",
        )
    assert excinfo.value.status_code == 503
    assert await conv_mod._reload_conversation(db, "conv_race") is not None
    await db.dispose()


def test_knowledge_config_uses_explicit_static_parameters():
    """所选 KB 时 knowledge_config 显式携带 static RAG 参数。"""
    from nexus_backend import conversations as conv_mod

    target = conv_mod._CreateTarget(
        agent_id="agent-1",
        chat_model_config={"type": "deepseek_chat", "model": "m"},
        knowledge=("kb-1",),
        skills=(),
        tools=(),
    )
    config = conv_mod._knowledge_config(target)
    assert config == {
        "knowledge_base_ids": ["kb-1"],
        "parameters": {
            "mode": "static",
            "top_k": 3,
            "emit_hint_event": True,
        },
    }
    # 未选 KB 时不安装中间件参数，保持官方默认行为。
    empty = conv_mod._CreateTarget(
        agent_id="agent-1",
        chat_model_config={"type": "deepseek_chat", "model": "m"},
        knowledge=(),
        skills=(),
        tools=(),
    )
    assert conv_mod._knowledge_config(empty) is None


@pytest.mark.asyncio
async def test_ready_clears_transient_create_intent(tmp_path):
    """转 ready 清理瞬时创建意图；未完成保留。"""
    from nexus_backend import conversations as conv_mod
    from nexus_backend.db import Conversation, Database

    db = Database(tmp_path / "intent.db")
    await db.init()
    async with db.session() as session:
        session.add(
            Conversation(
                id="conv_intent",
                owner=OWNER,
                employee_id="emp_x",
                agent_id=None,
                session_id="sess-1",
                session_marker="nexus-conv-conv_intent",
                title="新对话",
                capability_snapshot={},
                create_intent={"agent_id": "agent-1"},
                client_key="k",
                status="creating",
            ),
        )
        await session.commit()

    await conv_mod._set_status(db, "conv_intent", status="creating")
    assert (await conv_mod._reload_conversation(db, "conv_intent")).create_intent

    await conv_mod._set_status(db, "conv_intent", status="incomplete")
    assert (await conv_mod._reload_conversation(db, "conv_intent")).create_intent

    await conv_mod._set_status(db, "conv_intent", status="ready")
    assert (
        await conv_mod._reload_conversation(db, "conv_intent")
    ).create_intent is None
    await db.dispose()


@pytest.mark.asyncio
async def test_verify_and_bind_rejects_knowledge_param_drift(tmp_path):
    """原生 Session 的 Knowledge 检索参数漂移：factory 读回不得返回 ready。"""
    from nexus_backend import conversations as conv_mod

    _MODEL = {
        "type": "deepseek_chat",
        "credential_id": "c1",
        "model": "deepseek-flash",
        "parameters": {},
    }

    class FakeAdapter:
        async def list_sessions(self, agent_id):
            return {
                "sessions": [
                    {
                        "session": {
                            "id": "sess-1",
                            "agent_id": agent_id,
                            "config": {
                                "name": "nexus-conv-test",
                                "workspace_id": "ws-1",
                                "chat_model_config": dict(_MODEL),
                                "knowledge_config": {
                                    "knowledge_base_ids": ["kb-1"],
                                    "parameters": {"mode": "agentic"},
                                },
                            },
                        },
                    },
                ],
                "total": 1,
            }

    target = conv_mod._CreateTarget(
        agent_id="agent-1",
        chat_model_config=dict(_MODEL),
        knowledge=("kb-1",),
        skills=(),
        tools=(),
    )
    with pytest.raises(conv_mod.ConversationError):
        await conv_mod._verify_and_bind(
            FakeAdapter(),
            target,
            "sess-1",
            "nexus-conv-test",
        )
