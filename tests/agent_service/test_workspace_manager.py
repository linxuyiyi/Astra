# -*- coding: utf-8 -*-
"""``SessionWorkspaceManager`` 物理隔离测试。

这些测试使用真实 ``LocalWorkspace``、真实文件系统与真实 SQLite
（``httpx.ASGITransport`` 走官方 ASGI 生命周期），不调用模型：

* 同员工两个 Session 的 workspace ID 不同，且实际物理目录不同；
* 在 A 真实写入的文件，B 通过公开 workspace 读取能力不可读；
* 关闭并重建 manager、以及重建官方 ASGI 应用（模拟真实进程重启）后，
  同一 workspace ID 重建同一目录并保留文件，B 仍不可读；
* 恶意 ``workspace_id`` 不造成路径穿越；
* 并发首次请求同一 ID 返回同一个 workspace；
* ``close``/``close_all`` 正常释放缓存。
"""
from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from agentscope.workspace import LocalWorkspace

from nexus_agent_service.app import build_app
from nexus_agent_service.config import load_config
from nexus_agent_service.workspace import (
    SessionWorkspaceManager,
    workspace_dir_name,
)

SENTINEL = b"session-a-private-sentinel"
SENTINEL_NAME = "a-private.txt"


def _config(tmp_path):
    return load_config(
        env={
            "AGENTSCOPE_DATA_DIR": str(tmp_path / "agentscope"),
            "AGENTSCOPE_MODEL_NAME": "deepseek-flash",
        },
    )


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _create_agent_and_sessions(client, user_id: str):
    create = await client.post(
        "/agent/",
        headers={"X-User-ID": user_id},
        json={
            "name": "物理隔离冒烟员工",
            "system_prompt": "你是物理隔离测试员工。",
            "chat_config": {
                "invite_config": {
                    "invitable": False,
                    "invite_description": None,
                },
            },
        },
    )
    create.raise_for_status()
    agent_id = create.json()["agent_id"]

    session_ids: list[str] = []
    for _ in range(2):
        created = await client.post(
            "/sessions/",
            headers={"X-User-ID": user_id},
            json={"agent_id": agent_id, "name": "隔离对话"},
        )
        created.raise_for_status()
        session_ids.append(created.json()["session_id"])

    views = (
        await client.get(
            "/sessions/",
            headers={"X-User-ID": user_id},
            params={"agent_id": agent_id},
        )
    ).json()["sessions"]
    workspace_ids = {
        v["session"]["id"]: v["session"]["config"]["workspace_id"]
        for v in views
        if v["session"]["id"] in session_ids
    }
    return agent_id, session_ids, workspace_ids


@pytest.mark.asyncio
async def test_same_agent_two_sessions_real_physical_isolation(tmp_path):
    """同员工双 Session：ID 不同且物理目录不同，A 写入文件 B 不可读。

    同时通过官方 ``/workspace/files`` HTTP 接口做一次真实读取核对。
    """
    config = _config(tmp_path)
    app = build_app(config)
    user_id = config.internal_user_id
    async with app.router.lifespan_context(app):
        async with await _client(app) as client:
            agent_id, (sid_a, sid_b), workspace_ids = (
                await _create_agent_and_sessions(client, user_id)
            )
            wsid_a, wsid_b = workspace_ids[sid_a], workspace_ids[sid_b]

            # 仅 ID 不同不足为凭，必须核对物理目录。
            assert wsid_a != wsid_b
            assert workspace_dir_name(wsid_a) != workspace_dir_name(wsid_b)

            manager = app.state.workspace_manager
            assert isinstance(manager, SessionWorkspaceManager)
            workspace_a = await manager.get_workspace(
                user_id,
                agent_id,
                sid_a,
                wsid_a,
            )
            workspace_b = await manager.get_workspace(
                user_id,
                agent_id,
                sid_b,
                wsid_b,
            )

            assert workspace_a.workdir != workspace_b.workdir
            assert workspace_a.workdir == manager.workdir_for(wsid_a)
            assert workspace_b.workdir == manager.workdir_for(wsid_b)
            basedir = os.path.realpath(manager.basedir)
            for workdir in (workspace_a.workdir, workspace_b.workdir):
                assert os.path.realpath(workdir).startswith(basedir + os.sep)

            # 在 A 真实写入（官方 LocalWorkspace backend 公开文件 API）。
            backend_a = workspace_a.get_backend()
            await backend_a.write_file(
                os.path.join(workspace_a.workdir, SENTINEL_NAME),
                SENTINEL,
            )
            assert await backend_a.file_exists(
                os.path.join(workspace_a.workdir, SENTINEL_NAME),
            )

            # B 的 workspace 目录中同一路径不存在，且读取失败。
            backend_b = workspace_b.get_backend()
            assert not await backend_b.file_exists(
                os.path.join(workspace_b.workdir, SENTINEL_NAME),
            )
            with pytest.raises(FileNotFoundError):
                await backend_b.read_file(
                    os.path.join(workspace_b.workdir, SENTINEL_NAME),
                )

            # 官方 /workspace/files：A 可读，B 不可读。
            read_a = await client.get(
                "/workspace/files",
                headers={"X-User-ID": user_id},
                params={
                    "agent_id": agent_id,
                    "session_id": sid_a,
                    "path": SENTINEL_NAME,
                },
            )
            assert read_a.status_code == 200, read_a.text
            assert read_a.content == SENTINEL

            read_b = await client.get(
                "/workspace/files",
                headers={"X-User-ID": user_id},
                params={
                    "agent_id": agent_id,
                    "session_id": sid_b,
                    "path": SENTINEL_NAME,
                },
            )
            assert read_b.status_code == 404

            listing_b = await client.get(
                "/workspace/directories",
                headers={"X-User-ID": user_id},
                params={
                    "agent_id": agent_id,
                    "session_id": sid_b,
                    "path": "",
                },
            )
            assert listing_b.status_code == 200
            names_b = {e["name"] for e in listing_b.json()["entries"]}
            assert SENTINEL_NAME not in names_b


@pytest.mark.asyncio
async def test_restart_rebuilds_same_path_and_preserves_isolation(tmp_path):
    """重建 manager 与重建官方 ASGI 应用后，A 文件仍在、B 仍不可读。"""
    config = _config(tmp_path)
    user_id = config.internal_user_id

    app_first = build_app(config)
    async with app_first.router.lifespan_context(app_first):
        async with await _client(app_first) as client:
            agent_id, (sid_a, sid_b), workspace_ids = (
                await _create_agent_and_sessions(client, user_id)
            )
            wsid_a, wsid_b = workspace_ids[sid_a], workspace_ids[sid_b]
            manager = app_first.state.workspace_manager
            workspace_a = await manager.get_workspace(
                user_id,
                agent_id,
                sid_a,
                wsid_a,
            )
            path_a = os.path.join(workspace_a.workdir, SENTINEL_NAME)
            first_dir_a = workspace_a.workdir
            first_dir_b = manager.workdir_for(wsid_b)
            await workspace_a.get_backend().write_file(path_a, SENTINEL)

    # 模拟真实进程重启：同一 data_dir 重新装配官方应用（新 manager）。
    app_restart = build_app(config)
    async with app_restart.router.lifespan_context(app_restart):
        restarted = app_restart.state.workspace_manager
        assert isinstance(restarted, SessionWorkspaceManager)
        # 同一 workspace ID 在重启后重建同一物理目录。
        assert restarted.workdir_for(wsid_a) == first_dir_a
        assert restarted.workdir_for(wsid_b) == first_dir_b

        workspace_a2 = await restarted.get_workspace(
            user_id,
            agent_id,
            sid_a,
            wsid_a,
        )
        assert workspace_a2.workdir == first_dir_a
        assert await workspace_a2.get_backend().file_exists(
            os.path.join(workspace_a2.workdir, SENTINEL_NAME),
        )

        async with await _client(app_restart) as client:
            read_a = await client.get(
                "/workspace/files",
                headers={"X-User-ID": user_id},
                params={
                    "agent_id": agent_id,
                    "session_id": sid_a,
                    "path": SENTINEL_NAME,
                },
            )
            assert read_a.status_code == 200, read_a.text
            assert read_a.content == SENTINEL

            read_b = await client.get(
                "/workspace/files",
                headers={"X-User-ID": user_id},
                params={
                    "agent_id": agent_id,
                    "session_id": sid_b,
                    "path": SENTINEL_NAME,
                },
            )
            assert read_b.status_code == 404


@pytest.mark.asyncio
async def test_malicious_workspace_id_cannot_escape_basedir(tmp_path):
    """异常/恶意 workspace_id 只映射为基于根目录的哈希目录。"""
    basedir = tmp_path / "workspaces"
    manager = SessionWorkspaceManager(basedir=str(basedir))
    basedir_real = os.path.realpath(str(basedir))

    for malicious in (
        "../../etc/passwd",
        "..",
        "/tmp/absolute-escape",
        "a/b\\c",
    ):
        workdir = await manager.get_workspace(
            "nexus-service",
            "agent-1",
            "session-1",
            malicious,
        )
        resolved = os.path.realpath(workdir.workdir)
        assert resolved.startswith(basedir_real + os.sep)
        assert workdir.workdir == manager.workdir_for(malicious)
        name = os.path.basename(workdir.workdir)
        assert name == workspace_dir_name(malicious)
        assert "/" not in name and "\\" not in name and name != ".."

    await manager.close_all()


@pytest.mark.asyncio
async def test_concurrent_same_id_initializes_once(tmp_path):
    """并发首次请求同一 workspace_id 返回同一个实例。"""
    manager = SessionWorkspaceManager(basedir=str(tmp_path / "workspaces"))

    results = await asyncio.gather(
        *(
            manager.get_workspace(
                "nexus-service",
                "agent-1",
                "session-1",
                "same-workspace-id",
            )
            for _ in range(8)
        ),
    )
    first = results[0]
    assert all(workspace is first for workspace in results)
    assert len(manager._workspaces) == 1  # noqa: SLF001 - 测试核对缓存计数

    await manager.close_all()


@pytest.mark.asyncio
async def test_close_evicts_then_reinitializes(tmp_path):
    """close 释放缓存；再次请求同一 ID 返回新的已初始化实例。"""
    manager = SessionWorkspaceManager(basedir=str(tmp_path / "workspaces"))
    args = ("nexus-service", "agent-1", "session-1", "ws-close")

    first = await manager.get_workspace(*args)
    await manager.close("ws-close")
    assert "ws-close" not in manager._workspaces  # noqa: SLF001

    second = await manager.get_workspace(*args)
    assert second is not first
    assert second.workdir == first.workdir

    await manager.close_all()
    assert manager._workspaces == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_paused_initialize_then_close_waits_and_never_resurrects(
    tmp_path,
    monkeypatch,
):
    """暂停 initialize 时并发 close/close_all：等待初始化结束，不复活/漏关。

    确定性交错：get_workspace 进入 initialize 后被 gate 暂停，此时
    close 与 close_all 必须先阻塞等待（生命周期锁被持有），恢复后关闭
    生效；缓存清空且实例已关闭，之后不再出现迟到发布。
    """
    entered = asyncio.Event()
    gate = asyncio.Event()
    original_initialize = LocalWorkspace.initialize

    async def gated_initialize(self) -> None:
        entered.set()
        await gate.wait()
        await original_initialize(self)

    monkeypatch.setattr(LocalWorkspace, "initialize", gated_initialize)

    manager = SessionWorkspaceManager(basedir=str(tmp_path / "workspaces"))
    wsid = "lifecycle-ws"
    args = ("nexus-service", "agent-1", "s-1", wsid)

    get_task = asyncio.create_task(manager.get_workspace(*args))
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    # 初始化已暂停且生命周期锁被持有，缓存中不得有半成品。
    assert wsid not in manager._workspaces  # noqa: SLF001

    close_task = asyncio.create_task(manager.close(wsid))
    close_all_task = asyncio.create_task(manager.close_all())
    await asyncio.sleep(0.05)
    # close/close_all 必须等待初始化，不能中途返回。
    assert not close_task.done()
    assert not close_all_task.done()

    gate.set()
    workspace = await asyncio.wait_for(get_task, timeout=5.0)
    await asyncio.wait_for(
        asyncio.gather(close_task, close_all_task),
        timeout=5.0,
    )

    # 不漏关、不复活：缓存清空，实例已关闭；稍后仍无迟到发布。
    assert manager._workspaces == {}  # noqa: SLF001
    assert workspace.is_alive is False
    await asyncio.sleep(0.05)
    assert manager._workspaces == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_close_waits_for_inflight_init_and_no_duplicate_instance(
    tmp_path,
    monkeypatch,
):
    """暂停 initialize 期间的新同 ID 请求不会产生同路径双活实例。"""
    entered = asyncio.Event()
    gate = asyncio.Event()
    original_initialize = LocalWorkspace.initialize
    created: list[LocalWorkspace] = []
    original_init = LocalWorkspace.__init__

    def tracking_init(self, **kwargs):  # noqa: ANN001
        original_init(self, **kwargs)
        created.append(self)

    async def gated_initialize(self) -> None:
        entered.set()
        await gate.wait()
        await original_initialize(self)

    monkeypatch.setattr(LocalWorkspace, "__init__", tracking_init)
    monkeypatch.setattr(LocalWorkspace, "initialize", gated_initialize)

    manager = SessionWorkspaceManager(basedir=str(tmp_path / "workspaces"))
    wsid = "dup-check-ws"
    args = ("nexus-service", "agent-1", "s-1", wsid)

    first_task = asyncio.create_task(manager.get_workspace(*args))
    await asyncio.wait_for(entered.wait(), timeout=5.0)

    # 初始化在途时的并发同 ID 请求：必须等待并复用同一个实例。
    second_task = asyncio.create_task(manager.get_workspace(*args))
    await asyncio.sleep(0.05)
    assert len(created) == 1

    gate.set()
    first = await asyncio.wait_for(first_task, timeout=5.0)
    second = await asyncio.wait_for(second_task, timeout=5.0)
    assert first is second
    assert len(created) == 1
    assert first.workdir == manager.workdir_for(wsid)

    await manager.close_all()


@pytest.mark.asyncio
async def test_missing_workspace_id_assigns_per_session(tmp_path):
    """未提供持久 ID 时按 PER_SESSION 分配，两次调用目录不同。"""
    manager = SessionWorkspaceManager(
        basedir=str(tmp_path / "workspaces"),
    )
    first = await manager.get_workspace("nexus-service", "agent-1", "s-1")
    second = await manager.get_workspace("nexus-service", "agent-1", "s-2")
    assert first.workspace_id != second.workspace_id
    assert first.workdir != second.workdir
    await manager.close_all()
