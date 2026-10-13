# -*- coding: utf-8 -*-
"""原生写屏障 middleware 的真实 ASGI + SQLite 验证。

这些测试使用真实官方 ``create_app`` lifespan、真实 ``AsyncSQLAlchemyStorage``
（SQLite）与真实 ASGI 调用，只对官方存储的 ``upsert_agent`` 注入**可控延迟**
以复现“客户端超时取消但原生写入仍在进行”的时序；不调用真实模型、不读取 Home。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from nexus_agent_service.app import build_app
from nexus_agent_service.config import load_config

USER_ID = "nexus-service"


def _config(tmp_path):
    return load_config(
        env={
            "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
        },
    )


def _managed_prompt(revision: int, name: str = "测试员工") -> str:
    return (
        f"你是 Nexus 数字员工「{name}」。\n"
        "[nexus-employee-id:emp-write-barrier]\n"
        f"[nexus-write-revision:{revision}]"
    )


def _managed_prompt_with_body_markers(
    revision: int,
    name: str = "测试员工",
) -> str:
    """职责/指令正文含干扰标记，规范末尾对才是事实源。"""
    return (
        f"你是 Nexus 数字员工「{name}」。\n"
        "职责正文含干扰 [nexus-write-revision:999] 与 "
        "[nexus-employee-id:emp-fake]\n"
        "[nexus-employee-id:emp-write-barrier]\n"
        f"[nexus-write-revision:{revision}]"
    )


def _headers(**extra) -> dict:
    headers = {"X-User-ID": USER_ID}
    headers.update(extra)
    return headers


async def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


async def _create_agent(client, prompt: str, name: str = "测试员工") -> str:
    response = await client.post(
        "/agent/",
        headers=_headers(),
        json={"name": name, "system_prompt": prompt},
    )
    assert response.status_code == 201, response.text
    return response.json()["agent_id"]


async def _native_prompt(app, agent_id: str) -> str:
    record = await app.state.storage.get_agent(USER_ID, agent_id)
    return record.data.system_prompt


def _gate_first_upsert(storage, gate: asyncio.Event, entered: asyncio.Event):
    """只阻塞第一次 ``upsert_agent``，用于复现超时取消下的在途写入。"""
    original = storage.upsert_agent
    calls = {"n": 0}

    async def wrapper(user_id, record):
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await gate.wait()
        return await original(user_id, record)

    storage.upsert_agent = wrapper


@pytest.mark.asyncio
async def test_serialized_patch_stale_rejected_and_final_is_latest(tmp_path):
    """A 受理后超时 → 重放 A 等待第一 A 退出 → B → 迟到旧 A 被拒绝。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            gate = asyncio.Event()
            entered = asyncio.Event()
            _gate_first_upsert(app.state.storage, gate, entered)

            # A（修订 2）被受理，进入原生写入后被客户端取消（模拟超时）。
            a_task = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={
                        "name": "A",
                        "system_prompt": _managed_prompt(2, "A"),
                    },
                ),
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            a_task.cancel()
            await asyncio.sleep(0.05)
            # 外层已取消，但真实写入任务仍持锁进行中。
            assert not a_task.done()

            # 重放 A（同修订 2）与新编辑 B（修订 3）都在锁上排队。
            replay_a = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={
                        "name": "A",
                        "system_prompt": _managed_prompt(2, "A"),
                    },
                ),
            )
            b_task = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "3"}),
                    json={
                        "name": "B",
                        "system_prompt": _managed_prompt(3, "B"),
                    },
                ),
            )
            await asyncio.sleep(0.05)
            # 都还在等待第一 A 真正退出释放锁。
            assert not replay_a.done()
            assert not b_task.done()

            gate.set()  # 允许第一 A 完成写入
            with pytest.raises(asyncio.CancelledError):
                await a_task
            replay_resp = await replay_a
            b_resp = await b_task
            assert replay_resp.status_code == 200, replay_resp.text
            assert b_resp.status_code == 200, b_resp.text

            # 迟到旧 A（修订 2 < 已接受 3）被写屏障拒绝。
            late = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                json={"name": "A", "system_prompt": _managed_prompt(2, "A")},
            )
            assert late.status_code == 409, late.text

            final = await _native_prompt(app, agent_id)
            assert "[nexus-write-revision:3]" in final
            assert "B" in final


@pytest.mark.asyncio
async def test_cancelled_patch_holds_lock_until_real_write_finishes(tmp_path):
    """原生 PATCH 被外层取消时，锁保持到真实写入结束，后续写排队其后。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            gate = asyncio.Event()
            entered = asyncio.Event()
            _gate_first_upsert(app.state.storage, gate, entered)

            a_task = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={"system_prompt": _managed_prompt(2)},
                ),
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            a_task.cancel()

            b_task = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "3"}),
                    json={"system_prompt": _managed_prompt(3)},
                ),
            )
            await asyncio.sleep(0.1)
            # B 必须等 A 的真实写入结束才能提交。
            assert not b_task.done()
            assert "[nexus-write-revision:2]" not in await _native_prompt(
                app,
                agent_id,
            )

            gate.set()
            with pytest.raises(asyncio.CancelledError):
                await a_task
            b_resp = await b_task
            assert b_resp.status_code == 200, b_resp.text

            final = await _native_prompt(app, agent_id)
            assert "[nexus-write-revision:3]" in final


@pytest.mark.asyncio
async def test_managed_header_and_prompt_revision_must_match(tmp_path):
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            missing_header = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(),
                json={"system_prompt": _managed_prompt(2)},
            )
            assert missing_header.status_code == 409, missing_header.text

            mismatch = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "5"}),
                json={"system_prompt": _managed_prompt(4)},
            )
            assert mismatch.status_code == 409, mismatch.text

            invalid_header = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "abc"}),
                json={"system_prompt": _managed_prompt(2)},
            )
            assert invalid_header.status_code == 409, invalid_header.text

            # 同修订幂等恢复（等于已持久化修订）可通过。
            idempotent = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "1"}),
                json={"system_prompt": _managed_prompt(1)},
            )
            assert idempotent.status_code == 200, idempotent.text

            advance = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                json={"system_prompt": _managed_prompt(2)},
            )
            assert advance.status_code == 200, advance.text

            stale = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "1"}),
                json={"system_prompt": _managed_prompt(1)},
            )
            assert stale.status_code == 409, stale.text


@pytest.mark.asyncio
async def test_unmanaged_agent_crud_stays_official(tmp_path):
    """未带 Nexus 员工标记的原生独立 CRUD 维持官方行为。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, "普通原生助手。", name="原生")

            renamed = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(),
                json={"name": "原生-改名"},
            )
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["data"]["name"] == "原生-改名"

            # 即使误带修订头，非托管 Agent 仍保持官方行为。
            with_header = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "1"}),
                json={"name": "原生-再改名"},
            )
            assert with_header.status_code == 200, with_header.text

            deleted = await client.delete(
                f"/agent/{agent_id}",
                headers=_headers(),
            )
            assert deleted.status_code == 204, deleted.text


@pytest.mark.asyncio
async def test_health_reports_fresh_generation_id_per_build(tmp_path):
    first = build_app(_config(tmp_path))
    second = build_app(_config(tmp_path))
    first_generation = first.state.nexus_generation_id
    second_generation = second.state.nexus_generation_id
    assert isinstance(first_generation, str) and first_generation
    assert isinstance(second_generation, str) and second_generation
    assert first_generation != second_generation

    async with first.router.lifespan_context(first):
        async with await _client(first) as client:
            health = (await client.get("/nexus/health")).json()
            assert health["generation_id"] == first_generation


@pytest.mark.asyncio
async def test_storage_read_failure_rejects_managed_patch_fail_closed(tmp_path):
    """原生读取瞬时失败不能视为非托管：返回脱敏 503 且低修订不写入。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            storage = app.state.storage
            original = storage.get_agent

            async def boom(user_id, aid):
                raise RuntimeError("native read failed: secret-token")

            storage.get_agent = boom
            try:
                response = await client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={"system_prompt": _managed_prompt(2)},
                )
            finally:
                storage.get_agent = original

            assert response.status_code == 503, response.text
            # 脱敏：不回显底层异常。
            assert "secret-token" not in response.text
            native = await _native_prompt(app, agent_id)
            assert "[nexus-write-revision:1]" in native
            assert "[nexus-write-revision:2]" not in native


@pytest.mark.asyncio
async def test_body_markers_do_not_confuse_canonical_revision(tmp_path):
    """正文含相同标记文本时，只认末尾规范对，正常编辑不被误拒。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(
                client,
                _managed_prompt_with_body_markers(1),
            )

            advance = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                json={
                    "system_prompt": _managed_prompt_with_body_markers(2),
                },
            )
            assert advance.status_code == 200, advance.text

            native = await _native_prompt(app, agent_id)
            assert native.rstrip().endswith("[nexus-write-revision:2]")

            stale = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "1"}),
                json={"system_prompt": _managed_prompt_with_body_markers(1)},
            )
            assert stale.status_code == 409, stale.text


@pytest.mark.asyncio
async def test_patch_cannot_swap_employee_marker(tmp_path):
    """修订写入只能更新同一稳定员工，不允许替换为另一员工标记。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))
            other_employee = _managed_prompt(2).replace(
                "emp-write-barrier",
                "emp-other",
            )
            swapped = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                json={"system_prompt": other_employee},
            )
            assert swapped.status_code == 409, swapped.text
            assert "emp-other" not in await _native_prompt(app, agent_id)


@pytest.mark.asyncio
async def test_high_water_promoted_before_native_blocks_late_low_revision(
    tmp_path,
):
    """高修订明确失败后，低修订也不能迟到覆盖；同修订允许重试。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            ),
            base_url="http://test",
        )
        async with client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            storage = app.state.storage
            original = storage.upsert_agent

            async def boom(user_id, record):
                raise RuntimeError("native write failed")

            storage.upsert_agent = boom
            try:
                failed = await client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={"system_prompt": _managed_prompt(2)},
                )
            finally:
                storage.upsert_agent = original
            assert failed.status_code >= 500, failed.text

            low = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "1"}),
                json={"system_prompt": _managed_prompt(1)},
            )
            assert low.status_code == 409, low.text

            retry = await client.patch(
                f"/agent/{agent_id}",
                headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                json={"system_prompt": _managed_prompt(2)},
            )
            assert retry.status_code == 200, retry.text
            assert (await _native_prompt(app, agent_id)).rstrip().endswith(
                "[nexus-write-revision:2]",
            )


@pytest.mark.asyncio
async def test_managed_create_requires_matching_generation(tmp_path):
    """托管 POST 携带代次头：不匹配 409 且不创建；匹配才放行。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            stale = await client.post(
                "/agent/",
                headers=_headers(
                    **{"X-Nexus-Service-Generation": "stale-generation"},
                ),
                json={"name": "托管", "system_prompt": _managed_prompt(1)},
            )
            assert stale.status_code == 409, stale.text
            listing = (await client.get("/agent/", headers=_headers())).json()
            assert listing["total"] == 0

            current = app.state.nexus_generation_id
            ok = await client.post(
                "/agent/",
                headers=_headers(
                    **{"X-Nexus-Service-Generation": current},
                ),
                json={"name": "托管", "system_prompt": _managed_prompt(1)},
            )
            assert ok.status_code == 201, ok.text
            listing = (await client.get("/agent/", headers=_headers())).json()
            assert listing["total"] == 1

            # 未带代次头的独立原生创建探针维持官方行为。
            unmanaged = await client.post(
                "/agent/",
                headers=_headers(),
                json={"name": "原生", "system_prompt": "普通原生助手。"},
            )
            assert unmanaged.status_code == 201, unmanaged.text


@pytest.mark.asyncio
async def test_delete_is_serialized_with_patch(tmp_path):
    """DELETE 与 PATCH 走同一 agent 锁：先读旧值的旧写不会在删除后重新 upsert。"""
    app = build_app(_config(tmp_path))
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id = await _create_agent(client, _managed_prompt(1))

            gate = asyncio.Event()
            entered = asyncio.Event()
            _gate_first_upsert(app.state.storage, gate, entered)

            patch_task = asyncio.create_task(
                client.patch(
                    f"/agent/{agent_id}",
                    headers=_headers(**{"X-Nexus-Write-Revision": "2"}),
                    json={"system_prompt": _managed_prompt(2)},
                ),
            )
            await asyncio.wait_for(entered.wait(), timeout=5)

            delete_task = asyncio.create_task(
                client.delete(f"/agent/{agent_id}", headers=_headers()),
            )
            await asyncio.sleep(0.05)
            assert not delete_task.done()

            gate.set()
            patch_resp = await patch_task
            assert patch_resp.status_code == 200, patch_resp.text
            delete_resp = await delete_task
            assert delete_resp.status_code == 204, delete_resp.text
            assert await app.state.storage.get_agent(USER_ID, agent_id) is None
