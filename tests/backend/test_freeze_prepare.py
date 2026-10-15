# -*- coding: utf-8 -*-
"""删除准备与只读冻结查询（OpenSpec 任务 3.2.3.1）。

覆盖：

* 内部删除准备 ``/api/internal/prepare-delete``：短事务落库 ``deleting`` 与
  稳定操作身份、可重试幂等、不覆盖既有身份、拒绝未确认创建/编辑、未知员工；
* 只读冻结查询 ``/api/internal/run-fact``：真实绑定、runnable/deleting、
  未知绑定/错误秘密/错误租户一律 fail-closed；
* 适配器 ``freeze_agent`` 请求形状（MockTransport，不冒充真实服务）。

这些是后端事实源与内部控制的合同测试；真实跨进程冻结/admission 见
agent-service 的冻结测试与隔离 process 冒烟脚本。
"""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from nexus_backend.adapter import AgentScopeHTTPClient
from nexus_backend.app import build_app
from nexus_backend.config import load_config as load_backend_config
from nexus_backend.db import Conversation, Employee

USER_ID = "nexus-service"
TOKEN = "test-internal-token"


def _backend_config(tmp_path):
    return load_backend_config(
        env={
            "NEXUS_BACKEND_DATA_DIR": str(tmp_path / "backend"),
            "NEXUS_BACKEND_PORT": "8799",
            "AGENTSCOPE_SERVICE_URL": "http://127.0.0.1:8766",
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
            "NEXUS_INTERNAL_TOKEN": TOKEN,
        },
    )


async def _seed_employee(backend, *, employee_id="emp-1", agent_id="agent-1",
                         sync_state="ready", enabled=True):
    async with backend.state.db.session() as session:
        session.add(
            Employee(
                id=employee_id,
                agent_id=agent_id,
                name=f"员工-{employee_id}",
                normalized_name=f"员工-{employee_id}".casefold(),
                domain=f"领域-{employee_id}",
                normalized_domain=f"领域-{employee_id}".casefold(),
                responsibilities="职责",
                instructions="指令",
                avatar="🤖",
                enabled=enabled,
                sync_state=sync_state,
                knowledge=[],
                skills=[],
                tools=[],
                write_revision=1,
            ),
        )
        await session.commit()


async def _seed_conversation(backend, *, session_id="sess-1", agent_id="agent-1",
                             employee_id="emp-1", status="ready"):
    async with backend.state.db.session() as session:
        session.add(
            Conversation(
                id=f"conv-{session_id}",
                owner="owner-1",
                employee_id=employee_id,
                agent_id=agent_id,
                session_id=session_id,
                session_marker=f"marker-{session_id}",
                title="会话",
                capability_snapshot={},
                status=status,
            ),
        )
        await session.commit()


async def _get_employee(backend, employee_id="emp-1") -> Employee | None:
    async with backend.state.db.session() as session:
        return await session.get(Employee, employee_id)


async def _client(backend):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=backend),
        base_url="http://backend.test",
    )


def _internal_headers(user=USER_ID, token=TOKEN):
    return {"X-Nexus-Internal-Token": token, "X-User-ID": user}


@pytest.mark.asyncio
async def test_prepare_delete_marks_deleting_with_stable_operation(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        async with await _client(backend) as client:
            resp = await client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": "emp-1"},
                headers=_internal_headers(),
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["employee_id"] == "emp-1"
            assert body["agent_id"] == "agent-1"
            assert body["sync_state"] == "deleting"
            operation_id = body["operation_id"]
            assert isinstance(operation_id, str) and operation_id

            # 重试幂等：同一操作身份，绝不覆盖。
            again = await client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": "emp-1"},
                headers=_internal_headers(),
            )
            assert again.status_code == 200
            assert again.json()["operation_id"] == operation_id

        row = await _get_employee(backend)
        assert row is not None
        assert row.sync_state == "deleting"
        assert row.delete_operation_id == operation_id
        assert row.enabled is False


@pytest.mark.asyncio
async def test_prepare_delete_rejects_unconfirmed_write(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend, sync_state="updating")
        async with await _client(backend) as client:
            resp = await client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": "emp-1"},
                headers=_internal_headers(),
            )
            assert resp.status_code == 409
        row = await _get_employee(backend)
        assert row is not None
        assert row.sync_state == "updating"
        assert row.delete_operation_id is None


@pytest.mark.asyncio
async def test_prepare_delete_unknown_employee_404(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        async with await _client(backend) as client:
            resp = await client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": "missing"},
                headers=_internal_headers(),
            )
            assert resp.status_code == 404


@pytest.mark.asyncio
async def test_prepare_delete_requires_internal_token(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        async with await _client(backend) as client:
            for headers in (
                {},
                _internal_headers(token="wrong"),
                _internal_headers(user="someone-else"),
            ):
                resp = await client.post(
                    "/api/internal/prepare-delete",
                    json={"employee_id": "emp-1"},
                    headers=headers,
                )
                assert resp.status_code in (403, 503)
        row = await _get_employee(backend)
        assert row is not None and row.sync_state == "ready"


@pytest.mark.asyncio
async def test_run_fact_runnable_and_binding(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        await _seed_conversation(backend)
        async with await _client(backend) as client:
            resp = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "agent-1", "session_id": "sess-1"},
                headers=_internal_headers(),
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["runnable"] is True
            assert body["deleting"] is False
            assert body["operation_id"] is None
            assert body["agent_id"] == "agent-1"
            assert body["employee_id"] == "emp-1"
            assert body["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_run_fact_reports_deleting_operation(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        await _seed_conversation(backend)
        async with await _client(backend) as client:
            prep = await client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": "emp-1"},
                headers=_internal_headers(),
            )
            operation_id = prep.json()["operation_id"]
            resp = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "agent-1", "session_id": "sess-1"},
                headers=_internal_headers(),
            )
            body = resp.json()
            assert body["runnable"] is False
            assert body["deleting"] is True
            assert body["operation_id"] == operation_id


@pytest.mark.asyncio
async def test_run_fact_unknown_or_mismatch_fails_closed(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        await _seed_conversation(backend)
        async with await _client(backend) as client:
            # 未知 session。
            unknown = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "agent-1", "session_id": "nope"},
                headers=_internal_headers(),
            )
            assert unknown.status_code == 403
            # agent 与 session 绑定不一致。
            mismatch = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "other-agent", "session_id": "sess-1"},
                headers=_internal_headers(),
            )
            assert mismatch.status_code == 403
            # 未知 agent（仅 agent_id）。
            unknown_agent = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "ghost"},
                headers=_internal_headers(),
            )
            assert unknown_agent.status_code == 403
            # 缺少任何标识。
            empty = await client.post(
                "/api/internal/run-fact",
                json={},
                headers=_internal_headers(),
            )
            assert empty.status_code == 400


@pytest.mark.asyncio
async def test_run_fact_requires_token_and_tenant(tmp_path):
    backend = build_app(_backend_config(tmp_path))
    async with backend.router.lifespan_context(backend):
        await _seed_employee(backend)
        await _seed_conversation(backend)
        async with await _client(backend) as client:
            no_token = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "agent-1", "session_id": "sess-1"},
            )
            assert no_token.status_code == 403
            wrong_user = await client.post(
                "/api/internal/run-fact",
                json={"agent_id": "agent-1", "session_id": "sess-1"},
                headers=_internal_headers(user="intruder"),
            )
            assert wrong_user.status_code == 403


@pytest.mark.asyncio
async def test_freeze_agent_adapter_request_shape(tmp_path):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["token"] = request.headers.get("X-Nexus-Internal-Token")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"execution_drained": True})

    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8766",
    )
    adapter = AgentScopeHTTPClient(
        "http://127.0.0.1:8766",
        USER_ID,
        client=upstream_client,
    )
    try:
        result = await adapter.freeze_agent(
            operation_id="del_1",
            agent_id="agent-1",
            session_ids=["s1", "s2"],
            token=TOKEN,
            timeout_secs=5.0,
        )
    finally:
        await upstream_client.aclose()
    assert result == {"execution_drained": True}
    assert captured["path"] == "/nexus/internal/freeze"
    assert captured["token"] == TOKEN
    assert captured["body"]["operation_id"] == "del_1"
    assert captured["body"]["session_ids"] == ["s1", "s2"]
    assert captured["body"]["timeout_secs"] == 5.0
