# -*- coding: utf-8 -*-
"""Nexus 持久写入观察索引测试（任务 3.2.3.1）。"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import suppress
from pathlib import Path

import aiosqlite
import pytest

from nexus_agent_service.persistence_index import (
    Binding,
    IndexEntry,
    PersistenceIndex,
    PersistenceIndexConflictError,
    PersistenceIndexCorruptError,
    PersistenceIndexError,
    PersistenceIndexNotFoundError,
)

SAMPLE_SHA256_A = "a" * 64
SAMPLE_SHA256_B = "b" * 64
SAMPLE_SHA256_C = "c" * 64


@pytest.fixture
def binding() -> Binding:
    return Binding(
        user_id="user-test-01",
        agent_id="agent-test-01",
        session_id="session-test-01",
    )


def _install_pause_hook(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[aiosqlite.Connection],
    pause_sql: str,
) -> asyncio.Event:
    """安装 aiosqlite 连接/执行钩子：SQL 命中 pause_sql 时置位事件并挂起，供取消测试使用。"""
    pause_event = asyncio.Event()
    real_connect = aiosqlite.connect

    async def hooked_connect(*args: object, **kwargs: object) -> aiosqlite.Connection:
        conn = await real_connect(*args, **kwargs)
        captured.append(conn)
        real_execute = conn.execute

        async def hooked_execute(sql: str, *exec_args: object, **exec_kwargs: object) -> object:
            if pause_sql in sql:
                pause_event.set()
                await asyncio.sleep(60)
            return await real_execute(sql, *exec_args, **exec_kwargs)

        monkeypatch.setattr(conn, "execute", hooked_execute)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", hooked_connect)
    return pause_event


async def _cleanup_captured_conns(captured: list[aiosqlite.Connection]) -> None:
    """测试夹具自我保护：关闭所有捕获的真实连接，避免 aiosqlite 工作线程泄漏。"""
    for conn in captured:
        with suppress(Exception):
            await conn.close()


async def test_create_pending_confirm_and_reopen(tmp_path: Path, binding: Binding) -> None:
    """真实 SQLite 创建 pending、确认并同目录关闭重开验证。"""
    async with PersistenceIndex(tmp_path) as idx:
        assert idx.db_path.exists()
        assert idx.marker_path.exists()

        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-001",
            digest=SAMPLE_SHA256_A,
        )
        assert isinstance(entry_id, str) and len(entry_id) > 0

        pending = await idx.unconfirmed(binding)
        assert len(pending) == 1
        assert pending[0].entry_id == entry_id
        assert pending[0].kind == "message"
        assert pending[0].subject_id == "msg-001"
        assert pending[0].digest == SAMPLE_SHA256_A
        assert pending[0].confirmed is False
        assert pending[0].binding == binding

        await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        pending_after = await idx.unconfirmed(binding)
        assert pending_after == []

    # 重启：同目录重新打开读取原确认状态
    async with PersistenceIndex(tmp_path) as reopened:
        reopened_pending = await reopened.unconfirmed(binding)
        assert reopened_pending == []

        entry = await reopened.get(entry_id)
        assert entry is not None
        assert entry.entry_id == entry_id
        assert entry.confirmed is True


async def test_synchronous_pragma_is_full(tmp_path: Path) -> None:
    """验证所有连接严格使用 PRAGMA synchronous=FULL（SQLite 值为 2）。"""
    async with PersistenceIndex(tmp_path) as idx:
        assert idx._conn is not None
        async with idx._conn.execute("PRAGMA synchronous;") as cur:
            row = await cur.fetchone()
            assert row is not None and row[0] == 2

    # 重开后同样使用 synchronous=FULL
    async with PersistenceIndex(tmp_path) as reopened:
        assert reopened._conn is not None
        async with reopened._conn.execute("PRAGMA synchronous;") as cur:
            row = await cur.fetchone()
            assert row is not None and row[0] == 2


async def test_unknown_digest_cannot_be_confirmed_until_set(tmp_path: Path, binding: Binding) -> None:
    """未知 digest 不可确认；通过 set_digest 填入后方可确认。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="state",
            subject_id="state-chk-1",
            digest=None,
        )

        pending = await idx.unconfirmed(binding)
        assert len(pending) == 1
        assert pending[0].digest is None

        # 未知 digest 拒绝确认
        with pytest.raises(PersistenceIndexConflictError, match="条目摘要未知"):
            await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        # 填入已知摘要
        await idx.set_digest(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        # 填入相同摘要允许幂等
        await idx.set_digest(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        # 此时可以确认
        await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)
        assert await idx.unconfirmed(binding) == []


async def test_digest_mismatch_rejected(tmp_path: Path, binding: Binding) -> None:
    """已知摘要不同必须拒绝 set_digest 与 confirm。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-diff-digest",
            digest=SAMPLE_SHA256_A,
        )

        with pytest.raises(PersistenceIndexConflictError, match="不一致"):
            await idx.set_digest(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_B)

        with pytest.raises(PersistenceIndexConflictError, match="不一致"):
            await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_B)


async def test_wrong_binding_rejected(tmp_path: Path, binding: Binding) -> None:
    """错 Session/agent/user 不能修改或确认条目。"""
    wrong_user = Binding("other-user", binding.agent_id, binding.session_id)
    wrong_agent = Binding(binding.user_id, "other-agent", binding.session_id)
    wrong_session = Binding(binding.user_id, binding.agent_id, "other-session")

    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-binding-check",
            digest=SAMPLE_SHA256_A,
        )

        for wrong in (wrong_user, wrong_agent, wrong_session):
            with pytest.raises(PersistenceIndexConflictError, match="绑定与当前会话不匹配"):
                await idx.confirm(binding=wrong, entry_id=entry_id, digest=SAMPLE_SHA256_A)

            with pytest.raises(PersistenceIndexConflictError, match="绑定与当前会话不匹配"):
                await idx.set_digest(binding=wrong, entry_id=entry_id, digest=SAMPLE_SHA256_A)

            assert await idx.unconfirmed(wrong) == []

        # 原绑定依然处于 pending
        original_pending = await idx.unconfirmed(binding)
        assert len(original_pending) == 1
        assert original_pending[0].entry_id == entry_id


async def test_old_failure_preserved_when_new_success_confirmed(
    tmp_path: Path, binding: Binding
) -> None:
    """旧失败与新成功分别保留：R1 未确认不因 R2 确认清除。"""
    async with PersistenceIndex(tmp_path) as idx:
        r1_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-retry-01",
            digest=SAMPLE_SHA256_A,
        )
        r2_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-retry-01",
            digest=SAMPLE_SHA256_A,
        )
        assert r1_id != r2_id

        # 仅确认 R2
        await idx.confirm(binding=binding, entry_id=r2_id, digest=SAMPLE_SHA256_A)

        # R1 仍然保留在 unconfirmed 中
        pending = await idx.unconfirmed(binding)
        assert len(pending) == 1
        assert pending[0].entry_id == r1_id


async def test_concurrent_begin_no_lost_entries(tmp_path: Path, binding: Binding) -> None:
    """并发多个 begin 不丢条目。"""
    async with PersistenceIndex(tmp_path) as idx:
        count = 25
        tasks = [
            idx.begin(
                binding=binding,
                kind="message",
                subject_id=f"msg-concurrent-{i}",
                digest=SAMPLE_SHA256_A,
            )
            for i in range(count)
        ]
        entry_ids = await asyncio.gather(*tasks)

        assert len(set(entry_ids)) == count
        pending = await idx.unconfirmed(binding)
        assert len(pending) == count
        pending_ids = {p.entry_id for p in pending}
        assert pending_ids == set(entry_ids)


async def test_confirm_commit_failure_rollback_keeps_pending(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """确认 commit 故障 rollback 后重开仍 pending，且同连接不留内存乐观确认。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-commit-fail",
            digest=SAMPLE_SHA256_A,
        )

        assert idx._conn is not None
        real_commit = idx._conn.commit

        async def injected_failing_commit() -> None:
            raise aiosqlite.OperationalError("disk I/O error (injected)")

        monkeypatch.setattr(idx._conn, "commit", injected_failing_commit)

        with pytest.raises(PersistenceIndexError, match="写事务执行失败"):
            await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        # 恢复 commit
        monkeypatch.setattr(idx._conn, "commit", real_commit)

        # 校验：同连接在 rollback 后读取必须依然是 pending
        pending_same_conn = await idx.unconfirmed(binding)
        assert len(pending_same_conn) == 1
        assert pending_same_conn[0].entry_id == entry_id
        assert pending_same_conn[0].confirmed is False

    # 关闭并重开实例，依然处于 pending
    async with PersistenceIndex(tmp_path) as reopened:
        reopened_pending = await reopened.unconfirmed(binding)
        assert len(reopened_pending) == 1
        assert reopened_pending[0].entry_id == entry_id
        assert reopened_pending[0].confirmed is False


async def test_confirm_cancelled_before_commit_rolls_back_and_subsequent_begin_does_not_flush(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """confirm 在 UPDATE 后 commit 前被取消，必须执行 rollback；同连接与重开均保持 pending，无关 begin 不顺带 commit。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-cancel-01",
            digest=SAMPLE_SHA256_A,
        )

        assert idx._conn is not None
        real_commit = idx._conn.commit
        pause_event = asyncio.Event()

        async def hung_commit() -> None:
            pause_event.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(idx._conn, "commit", hung_commit)

        # 启动 confirm 任务并在 commit 挂起时取消它
        confirm_task = asyncio.create_task(
            idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)
        )
        await pause_event.wait()
        confirm_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await confirm_task

        # 恢复正常 commit
        monkeypatch.setattr(idx._conn, "commit", real_commit)

        # 1. 验证同连接上未确认列表依然包含该 entry，get 也是 confirmed=False
        same_conn_pending = await idx.unconfirmed(binding)
        assert len(same_conn_pending) == 1
        assert same_conn_pending[0].entry_id == entry_id
        entry_status = await idx.get(entry_id)
        assert entry_status is not None and entry_status.confirmed is False

        # 2. 写入一个无关的新条目（会触发真实 commit），检验不会顺带把旧条目刷成 confirmed=True
        unrelated_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-unrelated-02",
            digest=SAMPLE_SHA256_B,
        )

    # 3. 重新打开数据库，检验原条目依然处于 pending 状态，绝不永久消失
    async with PersistenceIndex(tmp_path) as reopened:
        reopened_pending = await reopened.unconfirmed(binding)
        reopened_ids = {p.entry_id for p in reopened_pending}
        assert entry_id in reopened_ids
        assert unrelated_id in reopened_ids

        e1 = await reopened.get(entry_id)
        assert e1 is not None and e1.confirmed is False


async def test_repeated_cancellation_during_rollback_does_not_corrupt(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """多次重复取消不能打断回滚完成，条目依然保留在 pending 状态。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-repeated-cancel",
            digest=SAMPLE_SHA256_A,
        )

        assert idx._conn is not None
        real_commit = idx._conn.commit
        commit_reached = asyncio.Event()

        async def pause_and_hang() -> None:
            commit_reached.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(idx._conn, "commit", pause_and_hang)

        task = asyncio.create_task(
            idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)
        )
        await commit_reached.wait()

        # 重复取消 3 次
        for _ in range(3):
            task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        monkeypatch.setattr(idx._conn, "commit", real_commit)

        pending = await idx.unconfirmed(binding)
        assert len(pending) == 1
        assert pending[0].entry_id == entry_id
        assert pending[0].confirmed is False


async def test_rollback_failure_marks_instance_poisoned_and_unusable(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回滚失败时连接已污染，必须将实例标记为不可用并关闭；后续读写显式报错，绝不伪造健康。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding,
            kind="message",
            subject_id="msg-rollback-fault",
            digest=SAMPLE_SHA256_A,
        )

        assert idx._conn is not None

        async def failing_commit() -> None:
            raise aiosqlite.OperationalError("commit fail")

        async def failing_rollback() -> None:
            raise aiosqlite.OperationalError("rollback fail")

        monkeypatch.setattr(idx._conn, "commit", failing_commit)
        monkeypatch.setattr(idx._conn, "rollback", failing_rollback)

        with pytest.raises(PersistenceIndexError):
            await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        # 此时实例已标记为 poisoned
        assert idx._poisoned is True
        assert idx._conn is None

        # 后续读写显式报错，绝不返回空列表
        with pytest.raises(PersistenceIndexError, match="不可用"):
            await idx.unconfirmed(binding)

        with pytest.raises(PersistenceIndexError, match="不可用"):
            await idx.begin(binding=binding, kind="message", subject_id="msg-2")

        with pytest.raises(PersistenceIndexError, match="不可用"):
            await idx.get(entry_id)


async def test_open_cancelled_closes_temporary_connection_proven_by_select_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """open 初始化过程中被取消时，捕获的真实局部连接已可靠关闭，用 SELECT 1 反证。"""
    captured: list[aiosqlite.Connection] = []
    pause_event = _install_pause_hook(monkeypatch, captured, "CREATE INDEX")

    idx = PersistenceIndex(tmp_path)
    open_task = asyncio.create_task(idx.open())
    try:
        await asyncio.wait_for(pause_event.wait(), timeout=5)
        open_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await open_task

        assert len(captured) == 1
        assert idx._conn is None
        assert idx._opened is False
        # 反证：由于 open 在取消时已执行可靠关闭，在捕获的连接上执行 SELECT 1 必须报错（aiosqlite 为 ValueError: no active connection）
        with pytest.raises((ValueError, sqlite3.ProgrammingError)):
            await captured[0].execute("SELECT 1;")
    finally:
        if not open_task.done():
            open_task.cancel()
            with suppress(BaseException):
                await open_task
        await _cleanup_captured_conns(captured)


async def test_open_cancelled_during_pragma_configuration_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """init 在 PRAGMA 配置阶段（连接已创建但尚未返回）被取消时，连接必须已登记并被可靠关闭。"""
    captured: list[aiosqlite.Connection] = []
    pause_event = _install_pause_hook(monkeypatch, captured, "PRAGMA journal_mode")

    idx = PersistenceIndex(tmp_path)
    open_task = asyncio.create_task(idx.open())
    try:
        await asyncio.wait_for(pause_event.wait(), timeout=5)
        open_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await open_task

        assert len(captured) == 1
        assert idx._conn is None
        assert idx._opened is False
        with pytest.raises((ValueError, sqlite3.ProgrammingError)):
            await captured[0].execute("SELECT 1;")
    finally:
        if not open_task.done():
            open_task.cancel()
            with suppress(BaseException):
                await open_task
        await _cleanup_captured_conns(captured)


async def test_verify_cancelled_during_pragma_configuration_closes_connection(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verify 在 PRAGMA 配置阶段被取消时，连接必须已登记并被可靠关闭，不泄漏工作线程。"""
    sub_dir = tmp_path / "verify_pragma_dir"
    async with PersistenceIndex(sub_dir) as idx:
        await idx.begin(
            binding=binding, kind="message", subject_id="msg-verify-pragma", digest=SAMPLE_SHA256_A
        )

    captured: list[aiosqlite.Connection] = []
    pause_event = _install_pause_hook(monkeypatch, captured, "PRAGMA journal_mode")

    reopened = PersistenceIndex(sub_dir)
    open_task = asyncio.create_task(reopened.open())
    try:
        await asyncio.wait_for(pause_event.wait(), timeout=5)
        open_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await open_task

        assert len(captured) == 1
        assert reopened._conn is None
        assert reopened._opened is False
        with pytest.raises((ValueError, sqlite3.ProgrammingError)):
            await captured[0].execute("SELECT 1;")
    finally:
        if not open_task.done():
            open_task.cancel()
            with suppress(BaseException):
                await open_task
        await _cleanup_captured_conns(captured)


async def test_close_failure_reports_error_and_retains_handle_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close 自身失败必须显式报错、标记实例不可用并保留真实句柄；恢复后重试可真正关闭。"""
    idx = PersistenceIndex(tmp_path)
    await idx.open()
    conn = idx._conn
    assert conn is not None

    real_close = conn.close

    async def failing_close() -> None:
        raise OSError("injected close failure")

    monkeypatch.setattr(conn, "close", failing_close)

    with pytest.raises(PersistenceIndexError, match="关闭索引数据库连接失败"):
        await idx.close()

    # 句柄未丢，实例不可用
    assert idx._conn is conn
    assert idx._opened is False
    assert idx._poisoned is True

    # 恢复真实 close 后重试成功，且连接确已关闭
    monkeypatch.setattr(conn, "close", real_close)
    await idx.close()

    assert idx._conn is None
    assert idx._opened is False

    with pytest.raises((ValueError, sqlite3.ProgrammingError)):
        await conn.execute("SELECT 1;")


async def test_rollback_task_cancelled_poisons_and_closes(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rollback 任务自身被取消也属清理失败：必须标记污染、关闭连接，后续读写显式报错。"""
    async with PersistenceIndex(tmp_path) as idx:
        entry_id = await idx.begin(
            binding=binding, kind="message", subject_id="msg-rb-cancel", digest=SAMPLE_SHA256_A
        )
        assert idx._conn is not None

        async def failing_commit() -> None:
            raise aiosqlite.OperationalError("commit fail")

        async def cancelled_rollback() -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(idx._conn, "commit", failing_commit)
        monkeypatch.setattr(idx._conn, "rollback", cancelled_rollback)

        with pytest.raises(PersistenceIndexError):
            await idx.confirm(binding=binding, entry_id=entry_id, digest=SAMPLE_SHA256_A)

        assert idx._poisoned is True
        assert idx._conn is None

        with pytest.raises(PersistenceIndexError, match="不可用"):
            await idx.unconfirmed(binding)

        with pytest.raises(PersistenceIndexError, match="不可用"):
            await idx.begin(binding=binding, kind="message", subject_id="msg-rb-cancel-2")


async def test_directory_fsync_failure_rejects_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """父目录 fsync 失败绝不能被忽略：初始化必须显式失败且不报告 ready。"""
    real_fsync = os.fsync
    call_count = {"n": 0}

    def counting_fsync(fd: int) -> None:
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise OSError("injected directory fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)

    idx = PersistenceIndex(tmp_path)
    with pytest.raises(PersistenceIndexError, match="初始化索引数据库失败"):
        await idx.open()

    assert call_count["n"] >= 2
    assert idx._opened is False
    assert idx._conn is None


async def test_failclosed_version_999_rejected_on_both_sides(
    tmp_path: Path, binding: Binding
) -> None:
    """marker.version 为 999 与 meta.schema_version 为 999 均严格拒绝（fail-closed）。"""
    async with PersistenceIndex(tmp_path) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

    marker = tmp_path / "persistence_index.marker"
    marker_content = json.loads(marker.read_text(encoding="utf-8"))

    # 1. 篡改 marker version 为 999
    marker_content["version"] = 999
    marker.write_text(json.dumps(marker_content), encoding="utf-8")

    with pytest.raises(PersistenceIndexCorruptError, match="不支持的 marker 版本"):
        async with PersistenceIndex(tmp_path):
            pass

    # 恢复 marker version 为 1
    marker_content["version"] = 1
    marker.write_text(json.dumps(marker_content), encoding="utf-8")

    # 2. 篡改 SQLite meta 中的 schema_version 为 999
    raw_conn = sqlite3.connect(tmp_path / "persistence_index.sqlite3")
    try:
        raw_conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version';")
        raw_conn.commit()
    finally:
        raw_conn.close()

    with pytest.raises(PersistenceIndexCorruptError, match="不支持的数据库 schema 版本"):
        async with PersistenceIndex(tmp_path):
            pass


async def test_fixed_directory_rejects_existing_file_and_missing_db_fails_closed(
    tmp_path: Path, binding: Binding
) -> None:
    """固定目录 API 拒绝已有文件路径，且内部 DB 丢失因 marker 存在继续 fail-closed，绝不换位置重置。"""
    # 1. 传入已有文件路径必须显式拒绝
    dummy_file = tmp_path / "regular_file.txt"
    dummy_file.write_text("hello", encoding="utf-8")

    with pytest.raises(ValueError, match="索引路径必须是目录"):
        PersistenceIndex(dummy_file)

    # 2. 正常初始化目录
    sub_dir = tmp_path / "sub_index_dir"
    async with PersistenceIndex(sub_dir) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-init", digest=SAMPLE_SHA256_A)

    # 3. 仅删除 DB 保留 marker
    db_file = sub_dir / "persistence_index.sqlite3"
    assert db_file.exists()
    db_file.unlink()

    # 再次打开必须严格拒绝，绝不在同一目录或新位置默默重新初始化
    with pytest.raises(PersistenceIndexCorruptError, match="缺少索引数据库文件"):
        async with PersistenceIndex(sub_dir):
            pass


async def test_failclosed_missing_marker(tmp_path: Path, binding: Binding) -> None:
    """初始化后 marker 文件丢失时 fail-closed。"""
    async with PersistenceIndex(tmp_path) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

    marker = tmp_path / "persistence_index.marker"
    assert marker.exists()
    marker.unlink()

    with pytest.raises(PersistenceIndexCorruptError, match="缺少初始化标记文件"):
        async with PersistenceIndex(tmp_path):
            pass


async def test_failclosed_marker_nonce_mismatch(tmp_path: Path, binding: Binding) -> None:
    """marker 与 SQLite meta 中的 nonce 错配时 fail-closed。"""
    async with PersistenceIndex(tmp_path) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

    marker = tmp_path / "persistence_index.marker"
    marker.write_text(json.dumps({"nonce": "tampered-nonce-0000", "version": 1}), encoding="utf-8")

    with pytest.raises(PersistenceIndexCorruptError, match="nonce 不一致"):
        async with PersistenceIndex(tmp_path):
            pass


async def test_failclosed_corrupted_db_and_incomplete_schema(tmp_path: Path, binding: Binding) -> None:
    """SQLite 损坏或 schema 不完整时 fail-closed，且不通过 CREATE IF NOT EXISTS 掩盖丢失。"""
    async with PersistenceIndex(tmp_path) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

    # 1. 损坏 SQLite 文件
    db_file = tmp_path / "persistence_index.sqlite3"
    db_file.write_bytes(b"NOT_A_VALID_SQLITE_DATABASE_HEADER")

    with pytest.raises(PersistenceIndexCorruptError):
        async with PersistenceIndex(tmp_path):
            pass

    # 2. 模拟丢表：用标准 sqlite3 库清空表（但保持有效 sqlite 文件和 marker），重开必须 failclosed
    another_dir = tmp_path / "dropped_table_dir"
    async with PersistenceIndex(another_dir) as idx2:
        await idx2.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

    raw_conn = sqlite3.connect(idx2.db_path)
    try:
        raw_conn.execute("DROP TABLE persistence_index;")
        raw_conn.commit()
    finally:
        raw_conn.close()

    with pytest.raises(PersistenceIndexCorruptError, match="缺少必要字段或表丢失"):
        async with PersistenceIndex(another_dir):
            pass


async def test_read_failure_raises_explicit_error(
    tmp_path: Path, binding: Binding, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引读失败抛出明确异常，绝不返回空列表假装健康。"""
    async with PersistenceIndex(tmp_path) as idx:
        await idx.begin(binding=binding, kind="message", subject_id="msg-1", digest=SAMPLE_SHA256_A)

        assert idx._conn is not None

        def failing_execute(*args: object, **kwargs: object) -> None:
            raise aiosqlite.OperationalError("simulated read fault")

        monkeypatch.setattr(idx._conn, "execute", failing_execute)

        with pytest.raises(PersistenceIndexError, match="读取未确认条目失败"):
            await idx.unconfirmed(binding)
