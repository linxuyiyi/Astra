# -*- coding: utf-8 -*-
"""员工管理业务测试（OpenSpec 任务 3.1）。

分两类：

1. 针对**真实官方 ASGI 服务 + 真实 SQLite** 的合同测试：验证真实
   ``POST/PATCH /agent/``、顶层 id 绑定、唯一性、原生姓名搜索与详情合并，
   不作为 mock 联调证据。
2. 明确标记的**模拟故障单元测试**：用 ``httpx.MockTransport`` 覆盖上游
   失败、超时未确认、写入意图持久化、并发串行、补偿/清理失败与 reconcile
   恢复分支，不冒充真实服务。

真实进程冒烟见 ``scripts/smoke_employees.py``。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import select

from nexus_backend import employees
from nexus_backend.adapter import AgentScopeHTTPClient
from nexus_backend.app import build_app
from nexus_backend.config import load_config as load_backend_config
from nexus_backend.db import Employee

USER_ID = "nexus-service"


def _backend_config(tmp_path):
    return load_backend_config(
        env={
            "NEXUS_BACKEND_DATA_DIR": str(tmp_path / "backend"),
            "NEXUS_BACKEND_PORT": "8799",
            "AGENTSCOPE_SERVICE_URL": "http://127.0.0.1:8766",
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )


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


@asynccontextmanager
async def real_stack(tmp_path):
    """真实官方 AgentScope ASGI + 真实后端 SQLite。"""
    from nexus_agent_service.app import build_app as build_agentscope_app
    from nexus_agent_service.config import (
        load_config as load_agentscope_config,
    )

    asc_config = load_agentscope_config(
        env={
            "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )
    upstream = build_agentscope_app(asc_config)
    upstream_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=upstream_client,
    )
    backend = build_app(_backend_config(tmp_path), adapter=adapter)
    async with upstream.router.lifespan_context(upstream):
        async with backend.router.lifespan_context(backend):
            client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=backend),
                base_url="http://backend.test",
            )
            try:
                yield backend, adapter, client, upstream
            finally:
                await client.aclose()
    await upstream_client.aclose()


@asynccontextmanager
async def mock_stack(tmp_path, handler):
    """模拟上游（MockTransport）+ 真实后端 SQLite。"""
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=upstream_client,
    )
    backend = build_app(_backend_config(tmp_path), adapter=adapter)
    async with backend.router.lifespan_context(backend):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=backend),
            base_url="http://backend.test",
        )
        try:
            yield backend, adapter, client
        finally:
            await client.aclose()
    await upstream_client.aclose()


async def _local_rows(backend) -> list[Employee]:
    async with backend.state.db.session() as session:
        return list((await session.execute(select(Employee))).scalars().all())


# ----------------------------------------------------------------------
# 模拟上游：可记录调用次数并切换故障模式（不冒充真实服务）
# ----------------------------------------------------------------------
def _mock_upstream():
    """返回 (state, handler)。state 维护原生 Agent 与调用计数/故障模式。

    ``post_mode``：``timeout`` / ``timeout_commit`` / ``error``（5xx 未提交）/
    ``error_commit``（已提交再抛 5xx）/ ``error_4xx``（明确未受理）/ 
    ``protocol_200_commit``（已提交但异常 200）/ ``missing_id``（201 缺 id）。

    ``patch_mode``：``timeout`` / ``timeout_commit`` / ``deferred``（延迟提交）/
    ``error``（5xx 未提交）/ ``error_commit``（已提交再抛 5xx）/ ``error_4xx`` /
    ``bad_response``（已提交但响应不可用）。
    """
    state = {
        "agents": {},
        "post_calls": 0,
        "patch_calls": 0,
        "get_fail": False,
        "get_fail_after_patch": False,
        "post_mode": None,
        "patch_mode": None,
        "deferred": [],
        "generation": "gen-1",
        "health_ready": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/nexus/health":
            if not state["health_ready"]:
                return httpx.Response(503, json={"ready": False})
            return httpx.Response(
                200,
                json={
                    "ready": True,
                    "generation_id": state["generation"],
                },
            )
        if method == "GET" and path == "/agent/":
            fail = state["get_fail"] or (
                state["get_fail_after_patch"] and state["patch_calls"] > 0
            )
            if fail:
                return httpx.Response(500, json={"detail": "boom"})
            agents = list(state["agents"].values())
            return httpx.Response(200, json={"agents": agents, "total": len(agents)})
        if method == "POST" and path == "/agent/":
            state["post_calls"] += 1
            body = json.loads(request.content.decode() or "{}")
            mode = state["post_mode"]
            if mode == "timeout":
                raise httpx.ConnectError("upstream timeout")
            if mode == "timeout_commit":
                _write_new_agent(state, body)
                raise httpx.ConnectError("upstream timeout")
            if mode == "error":
                return httpx.Response(500, json={"detail": "boom"})
            if mode == "error_commit":
                _write_new_agent(state, body)
                return httpx.Response(500, json={"detail": "boom"})
            if mode == "error_4xx":
                return httpx.Response(422, json={"detail": "rejected"})
            if mode == "protocol_200_commit":
                _write_new_agent(state, body)
                return httpx.Response(200, json={"agent_id": "ignored"})
            if mode == "missing_id":
                return httpx.Response(201, json={})
            agent_id = _write_new_agent(state, body)
            return httpx.Response(201, json={"agent_id": agent_id})
        if method == "PATCH" and path.startswith("/agent/"):
            state["patch_calls"] += 1
            agent_id = path.rsplit("/", 1)[1]
            body = json.loads(request.content.decode() or "{}")
            mode = state["patch_mode"]
            if mode == "timeout":
                raise httpx.ConnectError("upstream timeout")
            if mode == "timeout_commit":
                if agent_id in state["agents"]:
                    _apply_patch(state["agents"][agent_id], body)
                raise httpx.ConnectError("upstream timeout")
            if mode == "deferred":
                state["deferred"].append((agent_id, body))
                raise httpx.ConnectError("upstream timeout")
            if mode == "error":
                return httpx.Response(500, json={"detail": "boom"})
            if mode == "error_4xx":
                return httpx.Response(422, json={"detail": "rejected"})
            if mode == "error_commit":
                if agent_id in state["agents"]:
                    _apply_patch(state["agents"][agent_id], body)
                return httpx.Response(500, json={"detail": "boom"})
            if mode == "bad_response":
                if agent_id in state["agents"]:
                    _apply_patch(state["agents"][agent_id], body)
                return httpx.Response(200, json={"unexpected": True})
            if agent_id not in state["agents"]:
                return httpx.Response(404, json={"detail": "not found"})
            agent = state["agents"][agent_id]
            _apply_patch(agent, body)
            return httpx.Response(200, json=agent)
        if method == "DELETE" and path.startswith("/agent/"):
            state["agents"].pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "unexpected"})

    return state, handler


def _flush_deferred(state) -> None:
    """把“延迟提交”的 PATCH 真正落原生，模拟原请求稍后才提交。"""
    for agent_id, body in state["deferred"]:
        if agent_id in state["agents"]:
            _apply_patch(state["agents"][agent_id], body)
    state["deferred"] = []


def _write_new_agent(state, body) -> str:
    agent_id = f"agent-{len(state['agents']) + 1}"
    state["agents"][agent_id] = {
        "id": agent_id,
        "data": {
            "id": f"data-{agent_id}",
            "name": body.get("name"),
            "system_prompt": body.get("system_prompt"),
        },
        "editable": True,
    }
    return agent_id


def _apply_patch(agent: dict, body: dict) -> None:
    if body.get("name") is not None:
        agent["data"]["name"] = body["name"]
    if body.get("system_prompt") is not None:
        agent["data"]["system_prompt"] = body["system_prompt"]


def _flaky_session(monkeypatch, backend, fail_on: int):
    """让第 fail_on 次 db.session() 打开时抛错，用于模拟本地保存失败。"""
    real = backend.state.db.session
    calls = {"n": 0}

    @asynccontextmanager
    async def flaky():
        calls["n"] += 1
        if calls["n"] == fail_on:
            raise RuntimeError("db down")
        async with real() as session:
            yield session

    monkeypatch.setattr(backend.state.db, "session", flaky)
    return real


def _marker_prompt(row: Employee) -> str:
    return employees.compile_system_prompt(
        employee_id=row.id,
        name=row.name,
        domain=row.domain,
        responsibilities=row.responsibilities,
        instructions=row.instructions,
        revision=row.write_revision,
    )


def test_parse_prompt_revision_only_accepts_canonical_trailing_pair():
    """只认末尾规范的“员工标记行 + 修订标记最后一行”对，正文不参与。"""
    canonical = employees.compile_system_prompt(
        employee_id="emp_x",
        name="甲",
        domain="测试领域",
        responsibilities="职责含干扰 [nexus-write-revision:999]。",
        instructions="指令含 [nexus-employee-id:emp-fake]。",
        revision=3,
    )
    assert employees.parse_prompt_revision(canonical) == 3
    assert employees.parse_prompt_markers(canonical)[0] == "emp_x"

    # 末尾只有修订标记、缺少规范员工标记行：不解析。
    assert employees.parse_prompt_revision("正文\n[nexus-write-revision:5]") is None
    # 正文中出现相似文本但无规范末尾对：不解析。
    assert employees.parse_prompt_revision("正文 [nexus-write-revision:5]") is None
    assert employees.parse_prompt_revision("") is None
    assert employees.parse_prompt_revision(None) is None


# ----------------------------------------------------------------------
# 真实官方 ASGI + SQLite 合同测试
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_list_get_search_and_patch_against_real_agentscope(
    tmp_path,
):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 201, response.text
        created = response.json()
        assert created["name"] == "星河"
        assert created["name_source"] == "native"
        assert created["enabled"] is True
        assert created["native_available"] is True
        assert created["sync_state"] == "ready"
        assert created["agent_id"]

        # 原生 Agent 真实存在，名称与编译 prompt 一致，且顶层 id 与 data.id 不同。
        listing = await adapter.list_agents()
        matched = next(
            a for a in listing["agents"] if a["id"] == created["agent_id"]
        )
        assert matched["data"]["name"] == "星河"
        assert matched["data"]["id"] != matched["id"]
        prompt = matched["data"]["system_prompt"]
        assert "星河" in prompt
        assert "测试自动化工程师" in prompt
        assert "负责自动化脚本开发与维护。" in prompt
        assert "收到消息后先判断能否直接回答。" in prompt
        assert employees.employee_marker(created["id"]) in prompt

        # 列表与名称/领域搜索。
        listed = (await client.get("/api/employees")).json()
        assert listed["total"] == 1
        assert listed["employees"][0]["id"] == created["id"]
        by_name = (
            await client.get("/api/employees", params={"q": "星"})
        ).json()
        assert by_name["total"] == 1
        by_domain = (
            await client.get("/api/employees", params={"q": "自动化"})
        ).json()
        assert by_domain["total"] == 1
        miss = (
            await client.get("/api/employees", params={"q": "不存在"})
        ).json()
        assert miss["total"] == 0

        # 详情来自原生名称。
        detail = (await client.get(f"/api/employees/{created['id']}")).json()
        assert detail["name"] == "星河"

        # 编辑职责 → 原生 prompt 真实更新。
        patched = await client.patch(
            f"/api/employees/{created['id']}",
            json={"responsibilities": "改为负责性能测试与容量评估。"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["responsibilities"] == (
            "改为负责性能测试与容量评估。"
        )
        refreshed = await adapter.find_agent(created["agent_id"])
        assert "改为负责性能测试与容量评估。" in refreshed["data"][
            "system_prompt"
        ]


@pytest.mark.asyncio
async def test_duplicate_name_and_domain_case_insensitive_real(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        first = await client.post("/api/employees", json=_valid_payload())
        assert first.status_code == 201

        dup_name = await client.post(
            "/api/employees",
            json=_valid_payload(
                name="星 河".replace(" ", ""),
                domain="其他领域",
            ),
        )
        assert dup_name.status_code == 409, dup_name.text
        assert "name" in dup_name.json()["detail"]["errors"]

        dup_domain = await client.post(
            "/api/employees",
            json=_valid_payload(
                name="知微",
                domain="测试自动化工程师",
            ),
        )
        assert dup_domain.status_code == 409, dup_domain.text
        assert "domain" in dup_domain.json()["detail"]["errors"]

        listing = await adapter.list_agents()
        assert listing["total"] == 1
        assert len(await _local_rows(backend)) == 1


@pytest.mark.asyncio
async def test_validation_errors_real(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        missing = await client.post(
            "/api/employees",
            json={"name": "星河"},
        )
        assert missing.status_code == 422
        assert set(missing.json()["detail"]["errors"]) >= {
            "domain",
            "responsibilities",
            "instructions",
        }

        long_name = await client.post(
            "/api/employees",
            json=_valid_payload(name="名" * 25),
        )
        assert long_name.status_code == 422
        assert "name" in long_name.json()["detail"]["errors"]

        long_domain = await client.post(
            "/api/employees",
            json=_valid_payload(domain="域" * 43),
        )
        assert long_domain.status_code == 422
        assert "domain" in long_domain.json()["detail"]["errors"]

        bad_avatar = await client.post(
            "/api/employees",
            json=_valid_payload(avatar="🐶"),
        )
        assert bad_avatar.status_code == 422
        assert "avatar" in bad_avatar.json()["detail"]["errors"]

        # 全部失败都不应产生本地行或原生 Agent。
        assert await _local_rows(backend) == []
        assert (await adapter.list_agents())["total"] == 0


@pytest.mark.asyncio
async def test_not_found_real(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        assert (await client.get("/api/employees/emp_missing")).status_code == 404
        missing_patch = await client.patch(
            "/api/employees/emp_missing",
            json={"name": "x"},
        )
        assert missing_patch.status_code == 404


@pytest.mark.asyncio
async def test_detail_reports_incomplete_when_native_missing(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        # 绕过 Nexus 直接删除原生 Agent：详情必须明确报告配置不完整。
        assert await adapter.delete_agent(created["agent_id"]) is True
        detail = await client.get(f"/api/employees/{created['id']}")
        assert detail.status_code == 502
        assert "原生" in detail.json()["detail"]

        listed = (await client.get("/api/employees")).json()
        assert listed["total"] == 1
        assert listed["employees"][0]["native_available"] is False
        assert listed["employees"][0]["sync_state"] == "incomplete"


@pytest.mark.asyncio
async def test_concurrent_create_compensates_orphan_native_agent(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        payload = _valid_payload(name="并发员工", domain="并发领域")
        first, second = await asyncio.gather(
            client.post("/api/employees", json=payload),
            client.post("/api/employees", json=payload),
        )
        statuses = sorted([first.status_code, second.status_code])
        assert statuses[0] == 201
        assert statuses[1] == 409, (first.text, second.text)

        # 只有一个本地记录，落败请求未创建孤立原生 Agent。
        assert len(await _local_rows(backend)) == 1
        listing = await adapter.list_agents()
        assert listing["total"] == 1


@pytest.mark.asyncio
async def test_search_and_display_follow_native_name(tmp_path):
    """原生姓名变化后按真实姓名搜索，本地索引值不冒充原生事实。"""
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        # 绕过 Nexus 直接改原生姓名（直接写官方存储，不经过写屏障 HTTP）：
        # 搜索与展示必须以原生为准。
        record = await upstream.state.storage.get_agent(
            USER_ID,
            created["agent_id"],
        )
        record.data.name = "青云"
        await upstream.state.storage.upsert_agent(USER_ID, record)

        by_new = (
            await client.get("/api/employees", params={"q": "青云"})
        ).json()
        assert by_new["total"] == 1
        item = by_new["employees"][0]
        assert item["name"] == "青云"
        assert item["name_source"] == "native"
        assert item["native_available"] is True

        by_old = (
            await client.get("/api/employees", params={"q": "星河"})
        ).json()
        # 本地仍存旧索引值，但搜索按原生姓名，旧名不命中。
        assert by_old["total"] == 0

        detail = (await client.get(f"/api/employees/{created['id']}")).json()
        assert detail["name"] == "青云"
        assert detail["name_source"] == "native"


@pytest.mark.asyncio
async def test_concurrent_edits_serialized_native_local_consistent(tmp_path):
    """同一员工两个并发编辑串行执行，最终原生 prompt 与本地业务完全一致。"""
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()

        first, second = await asyncio.gather(
            client.patch(
                f"/api/employees/{created['id']}",
                json={"responsibilities": "并发职责甲。"},
            ),
            client.patch(
                f"/api/employees/{created['id']}",
                json={"instructions": "并发指令乙。"},
            ),
        )
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text

        rows = await _local_rows(backend)
        assert len(rows) == 1
        row = rows[0]
        assert row.sync_state == "ready"
        native = await adapter.find_agent(created["agent_id"])
        expected = employees.compile_system_prompt(
            employee_id=row.id,
            name=row.name,
            domain=row.domain,
            responsibilities=row.responsibilities,
            instructions=row.instructions,
            revision=row.write_revision,
        )
        assert native["data"]["system_prompt"] == expected


@pytest.mark.asyncio
async def test_extra_fields_are_rejected(tmp_path):
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        # 尚未实现的资源/启停操作不能被静默接受。
        post_extra = await client.post(
            "/api/employees",
            json=_valid_payload(name="越界", domain="越界领域")
            | {"resources": ["k1"]},
        )
        assert post_extra.status_code == 422
        post_enabled = await client.post(
            "/api/employees",
            json=_valid_payload(name="越界2", domain="越界领域2")
            | {"enabled": False},
        )
        assert post_enabled.status_code == 422
        patch_enabled = await client.patch(
            f"/api/employees/{created['id']}",
            json={"enabled": False},
        )
        assert patch_enabled.status_code == 422
        patch_unknown = await client.patch(
            f"/api/employees/{created['id']}",
            json={"knowledge": ["kb1"]},
        )
        assert patch_unknown.status_code == 422


@pytest.mark.asyncio
async def test_business_edit_with_markers_in_body_uses_canonical_revision(
    tmp_path,
):
    """职责/指令正文含标记文本时，真实业务编辑仍正常，规范末尾对是事实源。"""
    async with real_stack(tmp_path) as (backend, adapter, client, upstream):
        created = (
            await client.post(
                "/api/employees",
                json=_valid_payload(
                    responsibilities=(
                        "职责正文含干扰标记 [nexus-write-revision:999]。"
                    ),
                    instructions=(
                        "指令正文含 [nexus-employee-id:emp-fake]。"
                    ),
                ),
            )
        ).json()
        assert created["sync_state"] == "ready", created

        native = await adapter.find_agent(created["agent_id"])
        prompt = native["data"]["system_prompt"]
        assert "干扰标记 [nexus-write-revision:999]。" in prompt
        assert employees.parse_prompt_revision(prompt) == employees.CREATE_REVISION
        assert employees.parse_prompt_markers(prompt)[0] == created["id"]

        patched = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "改为新的工作指令，正文含 [nexus-write-revision:7]。"},
        )
        assert patched.status_code == 200, patched.text

        rows = await _local_rows(backend)
        expected_revision = rows[0].write_revision
        assert expected_revision == employees.CREATE_REVISION + 1
        refreshed = await adapter.find_agent(created["agent_id"])
        assert employees.parse_prompt_revision(
            refreshed["data"]["system_prompt"],
        ) == expected_revision
        assert "改为新的工作指令" in refreshed["data"]["system_prompt"]


# ----------------------------------------------------------------------
# 模拟故障单元测试（MockTransport；不冒充真实联调）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_create_definite_4xx_clears_placeholder(tmp_path):
    """仅明确未受理的 4xx 才清理占位，允许用户直接重试。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "error_4xx"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post(
            "/api/employees",
            json=_valid_payload(),
        )
        assert response.status_code == 502, response.text
        assert await _local_rows(backend) == []
        assert state["post_calls"] == 1
        assert state["agents"] == {}


@pytest.mark.asyncio
async def test_create_5xx_committed_keeps_placeholder_no_second_post(tmp_path):
    """已提交再抛 HTTP 500 视为结果未知：保留占位，重试不产生第二原生。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "error_commit"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        employee_id = detail["employee_id"]
        assert detail["sync_state"] == "creating"
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "creating"
        assert rows[0].agent_id is None
        assert state["post_calls"] == 1
        assert len(state["agents"]) == 1

        # 重试同 payload 命中占位唯一约束，不产生第二个原生 Agent。
        retry = await client.post("/api/employees", json=_valid_payload())
        assert retry.status_code == 409, retry.text
        assert state["post_calls"] == 1
        assert len(state["agents"]) == 1

        # 严格按标记核对，绑定后恢复 ready，绝不第二次 POST。
        reconciled = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        body = reconciled.json()
        assert body["sync_state"] == "ready"
        assert body["agent_id"] == "agent-1"
        assert state["post_calls"] == 1


@pytest.mark.asyncio
async def test_create_anomalous_200_committed_keeps_placeholder(tmp_path):
    """异常 200 协议响应也可能已创建：保留占位，核对标记恢复，不重发。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "protocol_200_commit"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 502, response.text
        employee_id = response.json()["detail"]["employee_id"]
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "creating"
        assert state["post_calls"] == 1

        retry = await client.post("/api/employees", json=_valid_payload())
        assert retry.status_code == 409, retry.text
        assert state["post_calls"] == 1
        assert len(state["agents"]) == 1

        reconciled = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["sync_state"] == "ready"
        assert state["post_calls"] == 1


@pytest.mark.asyncio
async def test_create_5xx_not_committed_keeps_placeholder_then_release(
    tmp_path,
):
    """未提交的 5xx 同样保留占位，核对未创建且服务代次变化后确认释放。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "error"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 502, response.text
        employee_id = response.json()["detail"]["employee_id"]
        assert len(await _local_rows(backend)) == 1

        unresolved = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert unresolved.status_code == 409

        # 同一服务代次下不允许靠确认猜测旧请求已结束。
        same_generation = await client.post(
            f"/api/employees/{employee_id}/reconcile",
            json={"confirm_not_created": True},
        )
        assert same_generation.status_code == 409, same_generation.text
        assert "代次" in same_generation.json()["detail"]["message"]
        assert len(await _local_rows(backend)) == 1

        # 服务重启后代次变化，才允许释放占位。
        state["generation"] = "gen-2"
        released = await client.post(
            f"/api/employees/{employee_id}/reconcile",
            json={"confirm_not_created": True},
        )
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "released"
        assert await _local_rows(backend) == []


@pytest.mark.asyncio
async def test_create_missing_agent_id_response_is_unknown(tmp_path):
    """201 但缺 agent_id 属异常协议响应，按结果未知保留占位，不冒认成功。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "missing_id"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 502, response.text
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "creating"
        assert rows[0].agent_id is None
        assert state["post_calls"] == 1


@pytest.mark.asyncio
async def test_create_generation_change_between_precheck_and_post_rejected(
    tmp_path,
):
    """预检记录代次后服务重启：托管 POST 携带旧代次头被拒，不创建原生 Agent。"""
    state = {
        "generation": "gen-1",
        "agents": {},
        "post_calls": 0,
        "post_generation_headers": [],
        "restart_after_precheck": True,
    }

    def _new_agent(body):
        agent_id = f"agent-{len(state['agents']) + 1}"
        state["agents"][agent_id] = {
            "id": agent_id,
            "data": {
                "id": f"data-{agent_id}",
                "name": body.get("name"),
                "system_prompt": body.get("system_prompt"),
            },
            "editable": True,
        }
        return agent_id

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/nexus/health":
            generation = state["generation"]
            if state["restart_after_precheck"]:
                # 预检返回后立刻“重启”到新代次。
                state["generation"] = "gen-2"
            return httpx.Response(
                200,
                json={"ready": True, "generation_id": generation},
            )
        if request.method == "GET" and path == "/agent/":
            agents = list(state["agents"].values())
            return httpx.Response(
                200,
                json={"agents": agents, "total": len(agents)},
            )
        if request.method == "POST" and path == "/agent/":
            state["post_calls"] += 1
            header = request.headers.get("X-Nexus-Service-Generation")
            state["post_generation_headers"].append(header)
            if header != state["generation"]:
                return httpx.Response(
                    409,
                    json={"detail": "generation mismatch"},
                )
            body = json.loads(request.content.decode() or "{}")
            return httpx.Response(201, json={"agent_id": _new_agent(body)})
        return httpx.Response(404, json={"detail": "unexpected"})

    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 502, response.text
        assert state["post_calls"] == 1
        # 携带的是预检得到的旧代次；受理实例已是新代次，故不创建。
        assert state["post_generation_headers"] == ["gen-1"]
        assert state["agents"] == {}
        assert await _local_rows(backend) == []


@pytest.mark.asyncio
async def test_create_confirmation_read_failure_keeps_pending_not_201(
    tmp_path,
):
    """创建已受理但读不到原生确认时返回未完成，不返回 201 假成功。"""
    state, handler = _mock_upstream()
    state["get_fail"] = True
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code != 201, response.text
        assert response.status_code == 502, response.text
        detail = response.json()["detail"]
        employee_id = detail["employee_id"]
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "creating"
        assert rows[0].agent_id is None

        state["get_fail"] = False
        reconciled = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["sync_state"] == "ready"


@pytest.mark.asyncio
async def test_create_recovery_race_reconcile_cannot_delete_inflight(tmp_path):
    """在途原生 POST 期间并发 reconcile(confirm) 不得删占位产生孤立 Agent。

    模拟阻塞创建：占位已持久化、原生 POST 尚未返回时，另一请求（已获知该员工
    ID）以 ``confirm_not_created`` 请求释放。员工锁必须覆盖整个 POST→bind，
    使核对/释放与之串行，绝不在 POST 成功前删除占位。
    """
    gate = asyncio.Event()
    entered = asyncio.Event()
    state = {"agents": {}, "post_calls": 0}

    def _new_agent(body):
        agent_id = f"agent-{len(state['agents']) + 1}"
        state["agents"][agent_id] = {
            "id": agent_id,
            "data": {
                "id": f"data-{agent_id}",
                "name": body.get("name"),
                "system_prompt": body.get("system_prompt"),
            },
            "editable": True,
        }
        return agent_id

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/nexus/health":
            return httpx.Response(
                200,
                json={"ready": True, "generation_id": "gen-race"},
            )
        if request.method == "GET" and request.url.path == "/agent/":
            agents = list(state["agents"].values())
            return httpx.Response(
                200, json={"agents": agents, "total": len(agents)}
            )
        if request.method == "POST" and request.url.path == "/agent/":
            state["post_calls"] += 1
            body = json.loads(request.content.decode() or "{}")
            entered.set()
            await gate.wait()  # 模拟在途原生 POST
            return httpx.Response(201, json={"agent_id": _new_agent(body)})
        return httpx.Response(404, json={"detail": "unexpected"})

    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        create_task = asyncio.create_task(
            client.post("/api/employees", json=_valid_payload())
        )
        await entered.wait()  # 已进入 POST 且占位已持久化

        rows = await _local_rows(backend)
        assert len(rows) == 1
        employee_id = rows[0].id
        assert rows[0].sync_state == "creating"

        reconcile_task = asyncio.create_task(
            client.post(
                f"/api/employees/{employee_id}/reconcile",
                json={"confirm_not_created": True},
            )
        )
        await asyncio.sleep(0.05)  # 让 reconcile 尝试获取同一员工锁
        gate.set()  # 允许在途 POST 完成

        create_resp = await create_task
        reconcile_resp = await reconcile_task

        assert create_resp.status_code == 201, create_resp.text
        # 核对与释放必须在同一锁内，不能在 POST 成功前删除占位。
        assert reconcile_resp.status_code == 200, reconcile_resp.text
        assert reconcile_resp.json().get("status") != "released"
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "ready"
        assert rows[0].agent_id is not None
        assert state["post_calls"] == 1
        assert len(state["agents"]) == 1


@pytest.mark.asyncio
async def test_create_conflict_prevents_native_create(tmp_path, monkeypatch):
    """本地唯一约束先于原生调用，落败并发不产生孤立原生 Agent。"""
    state, handler = _mock_upstream()
    monkeypatch.setattr(
        employees,
        "_find_conflict",
        _no_conflict,
    )
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        async with backend.state.db.session() as session:
            session.add(
                Employee(
                    id="emp_existing",
                    agent_id="agent-existing",
                    name="星河",
                    normalized_name="星河",
                    domain="既有领域",
                    normalized_domain="既有领域",
                    responsibilities="r",
                    instructions="i",
                    avatar="🤖",
                    enabled=True,
                ),
            )
            await session.commit()

        response = await client.post(
            "/api/employees",
            json=_valid_payload(),
        )
        assert response.status_code == 409, response.text
        assert state["post_calls"] == 0
        assert len(await _local_rows(backend)) == 1


async def _no_conflict(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_create_timeout_persists_pending_then_reconcile(tmp_path):
    """创建超时保留占位与稳定 ID，不自动重发，核对标记后恢复 ready。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "timeout"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post(
            "/api/employees",
            json=_valid_payload(),
        )
        assert response.status_code == 504, response.text
        detail = response.json()["detail"]
        employee_id = detail["employee_id"]
        assert detail["sync_state"] == "creating"

        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].id == employee_id
        assert rows[0].agent_id is None
        assert rows[0].sync_state == "creating"
        assert rows[0].write_intent is not None
        assert state["post_calls"] == 1

        # 模拟原生其实已创建：注入带稳定标记的真实记录。
        row = rows[0]
        state["agents"]["agent-real"] = {
            "id": "agent-real",
            "data": {
                "id": "data-real",
                "name": row.name,
                "system_prompt": _marker_prompt(row),
            },
            "editable": True,
        }
        reconciled = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        body = reconciled.json()
        assert body["sync_state"] == "ready"
        assert body["agent_id"] == "agent-real"
        assert state["post_calls"] == 1  # 未第二次 POST


@pytest.mark.asyncio
async def test_create_reconcile_ignores_other_agent_body_marker_reference(
    tmp_path,
):
    """另一员工正文引用待绑定员工标记但末尾是它自己的 ID：不得错误绑定。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "timeout"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 504, response.text
        employee_id = response.json()["detail"]["employee_id"]

        # 旁观员工正文含待绑定员工的规范标记文本，但其规范末尾是自身 ID。
        other_prompt = employees.compile_system_prompt(
            employee_id="emp-other",
            name="旁观员工",
            domain="其他领域",
            responsibilities=(
                "职责正文引用了 "
                + employees.employee_marker(employee_id)
                + " 这个待核对员工。"
            ),
            instructions="保持独立。",
            revision=7,
        )
        state["agents"]["agent-other"] = {
            "id": "agent-other",
            "data": {
                "id": "data-other",
                "name": "旁观员工",
                "system_prompt": other_prompt,
            },
            "editable": True,
        }

        reconciled = await client.post(f"/api/employees/{employee_id}/reconcile")
        assert reconciled.status_code == 409, reconciled.text
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].agent_id is None
        assert rows[0].sync_state == "creating"
        # 旁观员工未被误删，也未被误当作本员工绑定。
        assert "agent-other" in state["agents"]


@pytest.mark.asyncio
async def test_create_reconcile_same_id_but_config_drift_marks_incomplete(
    tmp_path,
):
    """末尾员工 ID 相同但原生配置偏离创建意图：标记 incomplete，不猜 ready。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "timeout"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post("/api/employees", json=_valid_payload())
        assert response.status_code == 504, response.text
        employee_id = response.json()["detail"]["employee_id"]
        rows = await _local_rows(backend)
        row = rows[0]

        drifted_prompt = _marker_prompt(row).replace(
            "负责自动化脚本开发与维护。",
            "职责已被别的写入改写。",
        )
        state["agents"]["agent-real"] = {
            "id": "agent-real",
            "data": {
                "id": "data-real",
                "name": row.name,
                "system_prompt": drifted_prompt,
            },
            "editable": True,
        }

        reconciled = await client.post(f"/api/employees/{employee_id}/reconcile")
        assert reconciled.status_code == 409, reconciled.text
        assert reconciled.json()["detail"]["sync_state"] == "incomplete"
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "incomplete"
        assert rows[0].agent_id is None


@pytest.mark.asyncio
async def test_create_timeout_not_found_requires_confirmation_to_release(
    tmp_path,
):
    state, handler = _mock_upstream()
    state["post_mode"] = "timeout"
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post(
            "/api/employees",
            json=_valid_payload(),
        )
        assert response.status_code == 504
        employee_id = response.json()["detail"]["employee_id"]

        unresolved = await client.post(
            f"/api/employees/{employee_id}/reconcile",
        )
        assert unresolved.status_code == 409
        assert "confirm_not_created" in unresolved.json()["detail"]["message"]
        assert len(await _local_rows(backend)) == 1

        # 同一代次：确认也不能释放，必须先重启服务使代次变化。
        same_generation = await client.post(
            f"/api/employees/{employee_id}/reconcile",
            json={"confirm_not_created": True},
        )
        assert same_generation.status_code == 409, same_generation.text
        assert len(await _local_rows(backend)) == 1

        state["generation"] = "gen-2"
        released = await client.post(
            f"/api/employees/{employee_id}/reconcile",
            json={"confirm_not_created": True},
        )
        assert released.status_code == 200, released.text
        assert released.json()["status"] == "released"
        assert await _local_rows(backend) == []
        assert state["post_calls"] == 1


@pytest.mark.asyncio
async def test_create_cleanup_failure_reports_unresolved(tmp_path, monkeypatch):
    """明确未受理但占位清理失败时，如实报告未完成，不宣称已清理。"""
    state, handler = _mock_upstream()
    state["post_mode"] = "error_4xx"

    async def fake_delete(db, employee_id):
        return False

    monkeypatch.setattr(employees, "_delete_placeholder", fake_delete)
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.post(
            "/api/employees",
            json=_valid_payload(),
        )
        assert response.status_code == 500, response.text
        detail = response.json()["detail"]
        assert "未完成" in detail["message"]
        assert detail["sync_state"] == "creating"
        rows = await _local_rows(backend)
        assert len(rows) == 1
        assert rows[0].sync_state == "creating"


@pytest.mark.asyncio
async def test_list_upstream_failure_returns_502(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        response = await client.get("/api/employees")
        assert response.status_code == 502
        assert "employees" not in response.json()


@pytest.mark.asyncio
async def test_update_timeout_intent_persisted_then_reconcile_committed(
    tmp_path,
):
    """编辑超时先持久化意图；原生已提交时核对后恢复 ready，不自动重试。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "timeout_commit"

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 504, response.text
        assert response.json()["detail"]["sync_state"] == "updating"
        assert state["patch_calls"] == 1

        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"
        assert rows[0].instructions == "收到消息后先判断能否直接回答。"

        # reconcile 必须走同修订幂等 PATCH 通过服务写屏障，再确认 desired。
        state["patch_mode"] = None
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        body = reconciled.json()
        assert body["sync_state"] == "ready"
        assert body["instructions"] == "新的工作指令。"
        assert state["patch_calls"] == 2  # 原始一次 + 幂等屏障一次


@pytest.mark.asyncio
async def test_update_timeout_reconcile_retries_same_target_recovers(tmp_path):
    """未知超时后原生仍是旧值：幂等重试相同目标并核对 desired 后才 ready。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "timeout"

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 504, response.text
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"
        assert rows[0].instructions == "收到消息后先判断能否直接回答。"
        assert state["patch_calls"] == 1

        # 允许原生恢复后重试；reconcile 对相同目标幂等重试并核对 desired。
        state["patch_mode"] = None
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        body = reconciled.json()
        assert body["sync_state"] == "ready"
        assert body["instructions"] == "新的工作指令。"
        assert state["patch_calls"] == 2  # 幂等重试一次
        native = await adapter.find_agent(created["agent_id"])
        assert "新的工作指令。" in native["data"]["system_prompt"]


@pytest.mark.asyncio
async def test_update_timeout_old_read_not_false_ready_delayed_commit(
    tmp_path,
):
    """延迟提交：单次旧值读取不产生 ready 假成功，desired 确认后才 ready。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "deferred"

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 504, response.text

        # 原生仍未提交（原请求稍后才提交）：一次旧值读取不得判为 ready。
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 504, reconciled.text
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"  # 仍 pending，未假 ready

        # 模拟原请求稍后提交成功，且服务恢复可写。
        _flush_deferred(state)
        state["patch_mode"] = None
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        body = reconciled.json()
        assert body["sync_state"] == "ready"
        assert body["instructions"] == "新的工作指令。"


@pytest.mark.asyncio
async def test_update_definite_4xx_reconciles_reverted(tmp_path):
    """明确未受理 4xx 且原生仍是精确旧快照时恢复到 ready，不返回假成功。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "error_4xx"

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 502, response.text
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "ready"
        assert rows[0].write_intent is None
        assert rows[0].instructions == "收到消息后先判断能否直接回答。"


@pytest.mark.asyncio
async def test_update_5xx_unknown_keeps_pending_not_reverted(tmp_path):
    """5xx 结果未知：不得凭旧值读取恢复 ready，保持 pending。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "error"

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 502, response.text
        assert response.json()["detail"]["sync_state"] == "updating"
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"
        assert rows[0].write_intent is not None

        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 504, reconciled.text
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"  # 未假 ready


@pytest.mark.asyncio
async def test_update_confirmation_unavailable_keeps_pending_not_ready(
    tmp_path,
):
    """PATCH 已提交但响应不可用且读不到确认：保持 pending，不返回 200 假成功。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "bad_response"
        # 编辑前的读取正常，PATCH 之后的确认读取失败。
        state["get_fail_after_patch"] = True

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code != 200, response.text
        assert response.status_code in (502, 504), response.text
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"  # 未落 ready 假成功
        assert rows[0].instructions == "收到消息后先判断能否直接回答。"

        state["get_fail_after_patch"] = False
        state["patch_mode"] = None
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["instructions"] == "新的工作指令。"


@pytest.mark.asyncio
async def test_update_intent_write_failure_does_not_call_native(
    tmp_path,
    monkeypatch,
):
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        # 第 1 次读取、第 2 次唯一性检查、第 3 次持久化意图 → 失败。
        real = _flaky_session(monkeypatch, backend, fail_on=3)

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 500, response.text
        assert "未调用原生" in response.json()["detail"]
        assert state["patch_calls"] == 0

        monkeypatch.setattr(backend.state.db, "session", real)
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "ready"
        assert rows[0].write_intent is None


@pytest.mark.asyncio
async def test_update_local_apply_failure_keeps_pending_then_reconcile(
    tmp_path,
    monkeypatch,
):
    """原生已提交但本地落库失败时保持未完成，reconcile 可恢复，不假称成功。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        # 第 1 读取、第 2 唯一性、第 3 持久化意图、第 4 落地 → 失败。
        real = _flaky_session(monkeypatch, backend, fail_on=4)

        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 500, response.text
        assert "未完成" in response.json()["detail"]["message"]
        assert state["patch_calls"] == 1

        monkeypatch.setattr(backend.state.db, "session", real)
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "updating"

        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 200, reconciled.text
        assert reconciled.json()["instructions"] == "新的工作指令。"
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "ready"


@pytest.mark.asyncio
async def test_update_ambiguous_reconcile_marks_incomplete(tmp_path):
    """原生既非旧值也非目标值时无法判定，标记 incomplete 而非假成功。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "timeout"
        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 504

        # 原生变成了第三种内容：既不是旧 prompt 也不是目标 prompt；服务对
        # 同修订幂等 PATCH 明确拒绝（4xx），无法核对为一致。
        state["agents"][created["agent_id"]]["data"][
            "system_prompt"
        ] = "无法归属的第三种内容"
        state["patch_mode"] = "error_4xx"
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 409, reconciled.text
        assert reconciled.json()["detail"]["sync_state"] == "incomplete"
        rows = await _local_rows(backend)
        assert rows[0].sync_state == "incomplete"


@pytest.mark.asyncio
async def test_reconcile_mark_failure_reports_error_not_success(
    tmp_path,
    monkeypatch,
):
    """标记未完成状态失败时明确报错，绝不谎称已保护成功。"""
    state, handler = _mock_upstream()
    async with mock_stack(tmp_path, handler) as (backend, adapter, client):
        created = (
            await client.post("/api/employees", json=_valid_payload())
        ).json()
        state["patch_mode"] = "timeout"
        response = await client.patch(
            f"/api/employees/{created['id']}",
            json={"instructions": "新的工作指令。"},
        )
        assert response.status_code == 504
        state["agents"][created["agent_id"]]["data"][
            "system_prompt"
        ] = "无法归属的第三种内容"
        # 同修订幂等 PATCH 被明确拒绝（4xx）→ 走标记 incomplete 分支。
        state["patch_mode"] = "error_4xx"

        # 第 1 次读取行、第 2 次写 incomplete → 失败。
        real = _flaky_session(monkeypatch, backend, fail_on=2)
        failed = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert failed.status_code == 500, failed.text
        assert "未被更改" in failed.json()["detail"]["message"]

        monkeypatch.setattr(backend.state.db, "session", real)
        rows = await _local_rows(backend)
        assert rows[0].sync_state != "ready"

        # 恢复后再次核对才能落到明确的 incomplete。
        reconciled = await client.post(
            f"/api/employees/{created['id']}/reconcile",
        )
        assert reconciled.status_code == 409
        assert reconciled.json()["detail"]["sync_state"] == "incomplete"
