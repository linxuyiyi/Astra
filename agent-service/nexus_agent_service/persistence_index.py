# -*- coding: utf-8 -*-
"""Nexus 持久写入观察索引（任务 3.2.3.1 观察基础）。

职责：
- 记录会话中消息或状态观察条目的提交证明元数据；
- 不存储消息或 AgentState 全文，不承担工作流/运行时调度状态；
- 基于 aiosqlite，采用单进程 asyncio 锁与极短 SQLite 事务；
- 失败安全与重启一致性：通过持久化 SQLite 与初始化 marker nonce 双向绑定实现 fail-closed；
- 条目级别隔离与原子提交：任何写变更（begin / set_digest / confirm）在返回成功前真实 commit；
  commit 失败或遭遇取消时自动执行 shielded rollback，连接不残留未提交状态；
  rollback 失败标记连接损坏不可用（fail-closed），避免脏连接读取伪造健康。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Literal, TypeVar

import aiosqlite

SUPPORTED_FORMAT_VERSION = 1
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
T = TypeVar("T")


def _validate_sha256(digest: str) -> str:
    """校验 SHA-256 十六进制摘要格式并返回规范小写字符串（脱敏报错）。"""
    if not isinstance(digest, str) or not _SHA256_HEX_PATTERN.match(digest):
        raise ValueError("无效的 SHA-256 摘要格式（需为 64 位十六进制）")
    return digest.lower()


def _resolve_paths(path: str | Path) -> tuple[Path, Path, Path]:
    """严格解析目录路径。拒绝已有文件路径，固定内部 db 与 marker 文件名。"""
    target = Path(path).resolve()
    if target.exists() and not target.is_dir():
        raise ValueError(f"索引路径必须是目录，收到已有非目录文件: {target.name}")
    db_path = target / "persistence_index.sqlite3"
    marker_path = target / "persistence_index.marker"
    return target, db_path, marker_path


def _write_marker_atomic(dir_path: Path, marker_path: Path, marker_data: str) -> None:
    """原子写入 marker 文件：写入临时文件、flush+fsync，replace 目标，并 fsync 父目录。

    任一步骤失败（含父目录 fsync）都必须向上抛出，绝不吞掉持久化失败伪装初始化成功；
    失败时尽力清理尚未 replace 的临时文件。
    """
    tmp_marker = marker_path.with_name(
        f"{marker_path.name}.tmp.{secrets.token_hex(6)}"
    )
    try:
        with open(tmp_marker, "w", encoding="utf-8") as f:
            f.write(marker_data)
            f.flush()
            os.fsync(f.fileno())

        tmp_marker.replace(marker_path)

        dir_fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            tmp_marker.unlink()
        except OSError:
            pass
        raise


async def _await_task_shielded(task: "asyncio.Task[object]") -> None:
    """等待 task 完成，屏蔽调用方的重复取消，但不读取也不吞掉 task 自身结果。

    task 的失败/取消由调用方通过 task.cancelled()/task.exception() 判定，
    从而不会把内部清理失败当成成功。
    """
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
        except BaseException:
            break


async def _safe_close_conn(conn: aiosqlite.Connection) -> bool:
    """可靠关闭 SQLite 连接，shield 保护且不受外层重复取消打断。

    返回 True 表示连接确已关闭；返回 False 表示 close 自身失败或被取消，连接可能仍
    存活，调用方必须保留真实句柄并可重试，绝不能当作关闭成功。
    """
    close_task = asyncio.ensure_future(conn.close())
    await _await_task_shielded(close_task)
    if close_task.cancelled():
        return False
    return close_task.exception() is None


class PersistenceIndexError(Exception):
    """持久写入观察索引基础异常。"""


class PersistenceIndexCorruptError(PersistenceIndexError):
    """索引文件损坏、缺失或元信息/结构不匹配（fail-closed）。"""


class PersistenceIndexConflictError(PersistenceIndexError):
    """绑定冲突、摘要冲突或状态不符合确认条件。"""


class PersistenceIndexNotFoundError(PersistenceIndexError):
    """指定的观察条目未找到。"""


@dataclass(frozen=True)
class Binding:
    """不可变会话身份绑定元数据。"""

    user_id: str
    agent_id: str
    session_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise ValueError("user_id 必须为非空字符串")
        if not isinstance(self.agent_id, str) or not self.agent_id.strip():
            raise ValueError("agent_id 必须为非空字符串")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id 必须为非空字符串")


@dataclass(frozen=True)
class IndexEntry:
    """不可变观察索引记录条目。"""

    entry_id: str
    user_id: str
    agent_id: str
    session_id: str
    kind: str
    subject_id: str
    digest: str | None
    confirmed: bool

    @property
    def binding(self) -> Binding:
        return Binding(
            user_id=self.user_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


class PersistenceIndex:
    """持久写入观察索引上下文管理器。"""

    def __init__(self, path: str | Path) -> None:
        self._dir_path, self._db_path, self._marker_path = _resolve_paths(path)
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None
        self._opened: bool = False
        self._poisoned: bool = False

    @property
    def dir_path(self) -> Path:
        return self._dir_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def marker_path(self) -> Path:
        return self._marker_path

    async def __aenter__(self) -> PersistenceIndex:
        await self.open()
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        await self.close()

    async def open(self) -> None:
        """打开并校验索引。初始化或校验均在 lock 保护下进行，遇取消或异常可靠关闭资源。"""
        async with self._lock:
            if self._opened:
                return
            if self._poisoned:
                raise PersistenceIndexError("索引连接已损坏且不可用")

            db_exists = self._db_path.exists()
            marker_exists = self._marker_path.exists()

            if not db_exists and not marker_exists:
                await self._init_new_database()
            elif db_exists and not marker_exists:
                raise PersistenceIndexCorruptError(
                    f"索引数据库已存在但缺少初始化标记文件，拒绝打开: {self._marker_path.name}"
                )
            elif not db_exists and marker_exists:
                raise PersistenceIndexCorruptError(
                    f"初始化标记文件已存在但缺少索引数据库文件，拒绝打开: {self._db_path.name}"
                )
            else:
                await self._verify_existing_database()

    async def _open_and_configure_connection(self, db_path: Path) -> None:
        """创建并配置连接：连接创建后立即登记所有权，配置在已持有连接的 scope 内完成。

        因此即便在 PRAGMA 配置阶段遭遇取消或异常，上层依然持有真实连接句柄并可靠回收，
        不会出现「helper 未返回、外层无句柄」导致的连接泄漏。
        """
        conn = await aiosqlite.connect(db_path)
        self._conn = conn
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=FULL;")

    def _require_conn(self) -> aiosqlite.Connection:
        conn = self._conn
        if conn is None:
            raise PersistenceIndexError("索引连接不可用")
        return conn

    async def _release_failed_connection(self) -> None:
        """回收打开/校验阶段失败或取消留下的连接。

        close 成功则清空句柄；close 失败则保留真实句柄并标记实例不可用，以便后续
        close 重试，绝不假关闭或伪造健康。
        """
        self._opened = False
        conn = self._conn
        if conn is None:
            return
        closed = await _safe_close_conn(conn)
        if closed:
            self._conn = None
        else:
            self._poisoned = True

    async def _init_new_database(self) -> None:
        """首次初始化数据库与 marker 绑定。任何异常或取消均可靠回收连接后原样重抛。"""
        self._dir_path.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(16)
        try:
            await self._open_and_configure_connection(self._db_path)
            conn = self._require_conn()
            await conn.execute(
                """
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE persistence_index (
                    entry_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    subject_id TEXT NOT NULL,
                    digest TEXT,
                    confirmed INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            await conn.execute(
                """
                CREATE INDEX idx_persistence_unconfirmed
                ON persistence_index (user_id, agent_id, session_id, confirmed);
                """
            )
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ('nonce', ?);",
                (nonce,),
            )
            await conn.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?);",
                (str(SUPPORTED_FORMAT_VERSION),),
            )
            await conn.commit()

            marker_data = json.dumps(
                {"nonce": nonce, "version": SUPPORTED_FORMAT_VERSION},
                indent=2,
            )
            _write_marker_atomic(self._dir_path, self._marker_path, marker_data)

            self._opened = True
        except BaseException as exc:
            await self._release_failed_connection()
            if isinstance(exc, (PersistenceIndexError, asyncio.CancelledError)):
                raise
            if isinstance(exc, Exception):
                raise PersistenceIndexError(
                    f"初始化索引数据库失败: {type(exc).__name__}"
                ) from exc
            raise

    async def _verify_existing_database(self) -> None:
        """验证已存在数据库与 marker 的完整性与一致性（fail-closed）。"""
        try:
            marker_raw = self._marker_path.read_text(encoding="utf-8")
            marker_data = json.loads(marker_raw)
            marker_nonce = marker_data.get("nonce")
            marker_version = marker_data.get("version")
            if not marker_nonce or not isinstance(marker_nonce, str):
                raise ValueError("标记文件缺少有效的 nonce")
            if marker_version != SUPPORTED_FORMAT_VERSION:
                raise PersistenceIndexCorruptError(
                    f"不支持的 marker 版本: {marker_version!r} (仅支持 {SUPPORTED_FORMAT_VERSION})"
                )
        except PersistenceIndexCorruptError:
            raise
        except Exception as exc:
            raise PersistenceIndexCorruptError(
                f"读取或解析标记文件失败: {type(exc).__name__}"
            ) from exc

        try:
            await self._open_and_configure_connection(self._db_path)
            conn = self._require_conn()
            async with conn.execute("PRAGMA quick_check;") as cursor:
                row = await cursor.fetchone()
                if not row or row[0] != "ok":
                    raise PersistenceIndexCorruptError("SQLite quick_check 未通过")

            async with conn.execute(
                "SELECT value FROM meta WHERE key = 'nonce';"
            ) as cursor:
                meta_row = await cursor.fetchone()
                if not meta_row or not meta_row[0]:
                    raise PersistenceIndexCorruptError("meta 表中缺少有效的 nonce 记录")
                db_nonce = meta_row[0]

            if db_nonce != marker_nonce:
                raise PersistenceIndexCorruptError(
                    "标记文件 nonce 与数据库 meta nonce 不一致，拒绝打开"
                )

            async with conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version';"
            ) as cursor:
                schema_row = await cursor.fetchone()
                if not schema_row or schema_row[0] != str(SUPPORTED_FORMAT_VERSION):
                    raise PersistenceIndexCorruptError(
                        f"不支持的数据库 schema 版本: {schema_row[0] if schema_row else None!r} (仅支持 {SUPPORTED_FORMAT_VERSION})"
                    )

            async with conn.execute("PRAGMA table_info(persistence_index);") as cursor:
                cols = [r[1] async for r in cursor]

            expected_cols = {
                "entry_id",
                "user_id",
                "agent_id",
                "session_id",
                "kind",
                "subject_id",
                "digest",
                "confirmed",
            }
            if not expected_cols.issubset(set(cols)):
                raise PersistenceIndexCorruptError(
                    "persistence_index 缺少必要字段或表丢失"
                )

            self._opened = True
        except BaseException as exc:
            await self._release_failed_connection()
            if isinstance(exc, (PersistenceIndexError, asyncio.CancelledError)):
                raise
            if isinstance(exc, Exception):
                raise PersistenceIndexCorruptError(
                    f"验证索引数据库失败: {type(exc).__name__}"
                ) from exc
            raise

    async def close(self) -> None:
        """关闭索引数据库连接。锁覆盖完整 cleanup，取消不中断关闭。

        close 失败时保留真实句柄并标记实例不可用，显式报错并允许后续重试，
        绝不吞掉失败后假装已关闭。
        """
        async with self._lock:
            conn = self._conn
            if conn is None:
                self._opened = False
                return
            closed = await _safe_close_conn(conn)
            if not closed:
                self._opened = False
                self._poisoned = True
                raise PersistenceIndexError(
                    "关闭索引数据库连接失败，实例不可用，句柄保留可重试关闭"
                )
            self._conn = None
            self._opened = False

    async def _rollback_or_poison(self) -> None:
        """可靠执行回滚。受 shield 保护且不受外层重复取消打断。

        回滚任务自身失败「或被取消」都必须视为清理失败：标记污染并关闭污染连接；
        绝不因 rollback_task.exception() 在取消时直接抛 CancelledError 而漏标。
        普通调用方取消在回滚完成后由上层原样传播。
        """
        conn = self._conn
        if conn is None:
            return

        async def _do_rollback() -> None:
            await conn.rollback()

        rollback_task = asyncio.ensure_future(_do_rollback())
        await _await_task_shielded(rollback_task)

        if not rollback_task.cancelled() and rollback_task.exception() is None:
            return

        self._poisoned = True
        self._opened = False
        if self._conn is conn:
            self._conn = None
        closed = await _safe_close_conn(conn)
        if not closed:
            self._conn = conn

    async def _execute_write_transaction(
        self, op: Callable[[aiosqlite.Connection], Awaitable[T]]
    ) -> T:
        """执行写事务并提交。遇到取消或异常可靠回滚后原样重抛。"""
        if self._poisoned:
            raise PersistenceIndexError("索引连接已损坏且不可用")
        if not self._opened or self._conn is None:
            raise PersistenceIndexError("索引未处于打开状态")

        try:
            res = await op(self._conn)
            await self._conn.commit()
            return res
        except (
            PersistenceIndexError,
            PersistenceIndexNotFoundError,
            PersistenceIndexConflictError,
        ):
            await self._rollback_or_poison()
            raise
        except asyncio.CancelledError:
            await self._rollback_or_poison()
            raise
        except BaseException as exc:
            await self._rollback_or_poison()
            if isinstance(exc, Exception):
                raise PersistenceIndexError(
                    f"写事务执行失败: {type(exc).__name__}"
                ) from exc
            raise

    async def begin(
        self,
        binding: Binding,
        kind: Literal["message", "state"],
        subject_id: str,
        digest: str | None = None,
    ) -> str:
        """开启一条观察写入记录，持久化返回独立生成的 entry_id。"""
        if not isinstance(binding, Binding):
            raise TypeError(f"binding 必须为 Binding 实例，收到: {type(binding).__name__}")
        if kind not in ("message", "state"):
            raise ValueError(f"kind 必须是 'message' 或 'state'，收到: {kind!r}")
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise ValueError("subject_id 必须为非空字符串")

        norm_digest: str | None = None
        if digest is not None:
            norm_digest = _validate_sha256(digest)

        entry_id = uuid.uuid4().hex

        async with self._lock:
            async def _do_begin(conn: aiosqlite.Connection) -> str:
                await conn.execute(
                    """
                    INSERT INTO persistence_index (
                        entry_id, user_id, agent_id, session_id, kind, subject_id, digest, confirmed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0);
                    """,
                    (
                        entry_id,
                        binding.user_id,
                        binding.agent_id,
                        binding.session_id,
                        kind,
                        subject_id,
                        norm_digest,
                    ),
                )
                return entry_id

            return await self._execute_write_transaction(_do_begin)

    async def set_digest(
        self,
        binding: Binding,
        entry_id: str,
        digest: str,
    ) -> None:
        """为尚未设定摘要的条目填入规范摘要。若已有摘要且不相同则拒绝。"""
        if not isinstance(binding, Binding):
            raise TypeError(f"binding 必须为 Binding 实例，收到: {type(binding).__name__}")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("entry_id 必须为非空字符串")
        norm_digest = _validate_sha256(digest)

        async with self._lock:
            async def _do_set_digest(conn: aiosqlite.Connection) -> None:
                async with conn.execute(
                    """
                    SELECT user_id, agent_id, session_id, digest, confirmed
                    FROM persistence_index
                    WHERE entry_id = ?;
                    """,
                    (entry_id,),
                ) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    raise PersistenceIndexNotFoundError(f"未找到指定的条目: {entry_id}")

                u_id, a_id, s_id, existing_digest, confirmed = row
                if (u_id, a_id, s_id) != (binding.user_id, binding.agent_id, binding.session_id):
                    raise PersistenceIndexConflictError("条目绑定与当前会话不匹配，拒绝修改")

                if existing_digest is not None:
                    if existing_digest != norm_digest:
                        raise PersistenceIndexConflictError(
                            f"条目已有已知摘要且与新摘要不一致，拒绝修改: {entry_id}"
                        )
                    return

                await conn.execute(
                    """
                    UPDATE persistence_index
                    SET digest = ?
                    WHERE entry_id = ?;
                    """,
                    (norm_digest, entry_id),
                )

            await self._execute_write_transaction(_do_set_digest)

    async def confirm(
        self,
        binding: Binding,
        entry_id: str,
        digest: str,
    ) -> None:
        """确认条目。仅在身份与已知摘要完全一致时确认并落盘。"""
        if not isinstance(binding, Binding):
            raise TypeError(f"binding 必须为 Binding 实例，收到: {type(binding).__name__}")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("entry_id 必须为非空字符串")
        norm_digest = _validate_sha256(digest)

        async with self._lock:
            async def _do_confirm(conn: aiosqlite.Connection) -> None:
                async with conn.execute(
                    """
                    SELECT user_id, agent_id, session_id, digest, confirmed
                    FROM persistence_index
                    WHERE entry_id = ?;
                    """,
                    (entry_id,),
                ) as cursor:
                    row = await cursor.fetchone()

                if not row:
                    raise PersistenceIndexNotFoundError(f"未找到指定的条目: {entry_id}")

                u_id, a_id, s_id, existing_digest, confirmed = row
                if (u_id, a_id, s_id) != (binding.user_id, binding.agent_id, binding.session_id):
                    raise PersistenceIndexConflictError("条目绑定与当前会话不匹配，拒绝确认")

                if existing_digest is None:
                    raise PersistenceIndexConflictError("条目摘要未知，不可确认")

                if existing_digest != norm_digest:
                    raise PersistenceIndexConflictError("条目已知摘要与确认摘要不一致，拒绝确认")

                await conn.execute(
                    """
                    UPDATE persistence_index
                    SET confirmed = 1
                    WHERE entry_id = ?;
                    """,
                    (entry_id,),
                )

            await self._execute_write_transaction(_do_confirm)

    async def unconfirmed(self, binding: Binding) -> list[IndexEntry]:
        """读回指定绑定下所有未确认条目。"""
        if not isinstance(binding, Binding):
            raise TypeError(f"binding 必须为 Binding 实例，收到: {type(binding).__name__}")

        async with self._lock:
            if self._poisoned:
                raise PersistenceIndexError("索引连接已损坏且不可用")
            if not self._opened or self._conn is None:
                raise PersistenceIndexError("索引未处于打开状态")

            try:
                async with self._conn.execute(
                    """
                    SELECT entry_id, user_id, agent_id, session_id, kind, subject_id, digest, confirmed
                    FROM persistence_index
                    WHERE user_id = ? AND agent_id = ? AND session_id = ? AND confirmed = 0
                    ORDER BY rowid ASC;
                    """,
                    (binding.user_id, binding.agent_id, binding.session_id),
                ) as cursor:
                    rows = await cursor.fetchall()

                return [
                    IndexEntry(
                        entry_id=r[0],
                        user_id=r[1],
                        agent_id=r[2],
                        session_id=r[3],
                        kind=r[4],
                        subject_id=r[5],
                        digest=r[6],
                        confirmed=bool(r[7]),
                    )
                    for r in rows
                ]
            except Exception as exc:
                raise PersistenceIndexError(
                    f"读取未确认条目失败: {type(exc).__name__}"
                ) from exc

    async def get(self, entry_id: str) -> IndexEntry | None:
        """按 entry_id 获取条目，不存在时返回 None。"""
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("entry_id 必须为非空字符串")

        async with self._lock:
            if self._poisoned:
                raise PersistenceIndexError("索引连接已损坏且不可用")
            if not self._opened or self._conn is None:
                raise PersistenceIndexError("索引未处于打开状态")

            try:
                async with self._conn.execute(
                    """
                    SELECT entry_id, user_id, agent_id, session_id, kind, subject_id, digest, confirmed
                    FROM persistence_index
                    WHERE entry_id = ?;
                    """,
                    (entry_id,),
                ) as cursor:
                    r = await cursor.fetchone()

                if not r:
                    return None

                return IndexEntry(
                    entry_id=r[0],
                    user_id=r[1],
                    agent_id=r[2],
                    session_id=r[3],
                    kind=r[4],
                    subject_id=r[5],
                    digest=r[6],
                    confirmed=bool(r[7]),
                )
            except Exception as exc:
                raise PersistenceIndexError(
                    f"读取条目失败: {type(exc).__name__}"
                ) from exc
