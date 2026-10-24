"""Worker 本地 ACP 驱动。仅保存绑定/受理凭据；消息历史仍在 DSH 原生磁盘。

SQLite 在调用 ACP 前同步提交 receipt 和 fence。网络重连只查询 receipt，绝不
重播 prompt；进程重启后的在途 receipt 是 unknown，必须先显式取消并核对所有权。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from app.worker_agent.dsh_acp.commands import COMMANDS
from app.worker_agent.dsh_acp.process import AcpProcessSupervisor
from app.worker_agent.dsh_acp.profile import (
    ACP_VERSION,
    DSH_VERSION,
    GatewayModel,
    prepare_profile,
)
from app.worker_agent.dsh_acp.skills import frozen_manifest, manifest
from app.worker_agent.dsh_acp.skills import snapshot as snapshot_skills
from app.worker_agent.dsh_acp.trace import read_trace_page

_ID = re.compile(r"[a-zA-Z0-9_-]{1,160}")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class DriverConfig:
    executable: Path
    state_root: Path
    workspace_root: Path
    gateway_url: str
    key_file: Path
    models: dict[str, dict]
    mcp_servers: tuple[dict, ...] = ()
    skills: dict[str, Path] = field(default_factory=dict)
    lock_file: Path = Path(__file__).resolve().parents[5] / "ops/runtime/dsh/package-lock.json"

    @classmethod
    def load(cls, path: Path, workspace_root: str) -> DriverConfig:
        if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid():
            raise ValueError("DSH 配置必须是当前用户的私有普通文件")
        value = json.loads(path.read_text())
        return cls(
            executable=Path(value["executable"]).resolve(strict=True),
            state_root=Path(value["state_root"]),
            workspace_root=Path(workspace_root).resolve(strict=True),
            gateway_url=value["gateway_url"],
            key_file=Path(value["key_file"]),
            models=value["models"],
            mcp_servers=tuple(value.get("mcp_servers", [])),
            skills={name: Path(path) for name, path in value.get("skills", {}).items()},
            lock_file=Path(value["lock_file"]),
        )


class DshAcpDriver:
    def __init__(self, config: DriverConfig, worker_id: str, *, supervisor_factory=AcpProcessSupervisor):
        self.config = config
        self.worker_id = worker_id
        self.factory = supervisor_factory
        root = config.state_root
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("DSH 状态目录必须是独立绝对目录")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_mode & 0o077 or root.stat().st_uid != os.getuid():
            raise ValueError("DSH 状态目录必须归当前用户独占")
        # 一个 Worker journal 只有一个写入者，避免两套 Host 把 receipt 错判为可恢复。
        import fcntl

        self._lock = os.open(root / ".worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock)
            raise RuntimeError("DSH Worker 状态目录已有写入者") from None
        db_path = root / "receipts.sqlite3"
        if db_path.is_symlink():
            raise ValueError("拒绝链接形式的受理账本")
        self.db = sqlite3.connect(db_path)
        os.chmod(db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bindings (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
                workspace TEXT NOT NULL, native_id TEXT, profile_hash TEXT NOT NULL,
                assembly_hash TEXT NOT NULL, model TEXT NOT NULL, fence INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL, stop_reason TEXT, submission_id TEXT, owner TEXT);
            CREATE TABLE IF NOT EXISTS receipts (
                binding_id TEXT NOT NULL, submission_id TEXT NOT NULL, fence INTEGER NOT NULL,
                generation INTEGER NOT NULL, payload_hash TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(binding_id, submission_id));
        """)
        self.db.execute("UPDATE bindings SET state='unknown' WHERE state IN ('running','preparing')")
        self.db.execute("UPDATE receipts SET state='unknown' WHERE state='running'")
        self.db.commit()
        self.hosts: dict[str, AcpProcessSupervisor] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.turns: dict[str, asyncio.Task] = {}
        self.operations: set[asyncio.Task] = set()
        self.events: dict[str, deque] = {}
        self.sequences: dict[str, int] = {}
        self.permissions: dict[str, dict] = {}
        self.connected = True
        self.stream_id = uuid4().hex
        self.protocol_checked_at = 0.0
        self.health_lock = asyncio.Lock()

    async def handle(self, name: str, data: dict, gate=lambda: None) -> dict:
        if name not in COMMANDS:
            raise ValueError("未知 DSH 命令")
        gate()
        # 已受理的副作用归驱动所有，WS 断线取消控制任务不会中断提交/取消的持久决策。
        task = asyncio.create_task(self._handle(name, data, gate))
        self.operations.add(task)

        def completed(operation):
            self.operations.discard(operation)
            if not operation.cancelled():
                operation.exception()  # 断线后无人等待的异常也不输出第三方原文。

        task.add_done_callback(completed)
        result = await asyncio.shield(task)
        from app.runtime.redaction import redact_data

        return redact_data(result, [self.config.key_file.read_text().strip()])

    async def _handle(self, name: str, data: dict, gate) -> dict:
        if name == "dsh_health":
            async with self.health_lock:
                return await self.health()
        binding_id = str(data.get("binding_id", ""))
        if not _ID.fullmatch(binding_id) or not _ID.fullmatch(str(data.get("task_id", ""))):
            raise ValueError("缺少明确的 Task/绑定标识")
        async with self.locks.setdefault(binding_id, asyncio.Lock()):
            gate()
            if name == "dsh_prepare":
                await self.prepare(data)
            if name == "dsh_lookup":
                row = self._binding(data, preparing=True)
                # 仅用于对账已持久绑定，不创建 Host，不返回会话内容或审批。
                return {
                    key: value
                    for key, value in self.snapshot(row).items()
                    if key not in {"events", "permissions"}
                }
            if (
                name == "dsh_cancel"
                and not self.db.execute(
                    "SELECT 1 FROM bindings WHERE id=?",
                    (binding_id,),
                ).fetchone()
            ):
                # 尚未到达的 prepare 也必须被 fence 拦截，不能在停止确认后新建 Host。
                fence = data.get("fence")
                if type(fence) is not int or fence < 1:
                    raise ValueError("取消 fence 无效")
                with self.db:
                    self.db.execute(
                        "INSERT INTO bindings(id,task_id,worker_id,workspace,profile_hash,"
                        "assembly_hash,model,fence,state) "
                        "VALUES(?,?,?,'','','','',?,'stopped')",
                        (binding_id, data["task_id"], self.worker_id, fence),
                    )
            row = self._binding(data, preparing=name == "dsh_prepare", canceling=name == "dsh_cancel")
            if name == "dsh_submit":
                await self.submit(row, data, gate=gate)
            elif name == "dsh_cancel":
                await self.cancel(row, data)
            elif name == "dsh_dispose":
                if row["state"] not in {"succeeded", "disposing"} or row["stop_reason"] != "end_turn":
                    raise RuntimeError("只允许关闭已正常结束的 ACP Host")
                await self.cancel(row, data, preserve_success=True)
            elif name == "dsh_permission":
                self.reply_permission(row, data)
            elif name == "dsh_trace":
                return await asyncio.to_thread(
                    read_trace_page,
                    self.config.state_root / row["id"],
                    Path(row["workspace"]),
                    row["native_id"],
                    cursor=int(data.get("cursor", 0)),
                    limit=int(data.get("limit", 40)),
                )
            view = self.snapshot(
                self._binding(
                    data,
                    preparing=name == "dsh_prepare",
                    canceling=name == "dsh_cancel",
                ),
                int(data.get("after", 0)),
            )
            if name == "dsh_submit":
                receipt = self.db.execute(
                    "SELECT submission_id,fence,generation,payload_hash,state FROM receipts "
                    "WHERE binding_id=? AND submission_id=?",
                    (row["id"], data["submission_id"]),
                ).fetchone()
                view["receipt"] = dict(receipt)
            return (
                view
                if name == "dsh_snapshot"
                else {key: value for key, value in view.items() if key not in {"events", "permissions"}}
            )

    async def health(self) -> dict:
        from app.worker_agent.dsh_acp.preflight import inspect_installation, inspect_worker_file_channel

        result = await asyncio.to_thread(
            inspect_installation,
            self.config.executable,
            self.config.lock_file,
        )
        file_channel = await asyncio.to_thread(inspect_worker_file_channel, self.config.workspace_root)
        if not file_channel["file_channel_verified"]:
            return {"engine": "dsh", "healthy": False, **result, **file_channel}
        for alias, spec in self.config.models.items():
            GatewayModel(self.config.gateway_url, alias, **spec)
        if not self.config.models:
            raise ValueError("DSH 没有登记 Gateway 模型")
        import httpx

        from app.worker_agent.inspection import _inspect_repository

        key = self.config.key_file
        if key.is_symlink() or key.stat().st_mode & 0o077 or key.stat().st_uid != os.getuid():
            raise ValueError("Gateway 调用密钥文件权限无效")
        secret = key.read_text().strip()
        async with httpx.AsyncClient(timeout=15) as client:
            catalog = await client.get(
                self.config.gateway_url.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {secret}"},
            )
            catalog.raise_for_status()
            enabled = {x["id"] for x in catalog.json().get("data", [])}
        skill_hashes = manifest(self.config.skills)
        # 包版本与 --version 都不能证明 ACP Host 的原生扩展可用。
        if time.monotonic() - self.protocol_checked_at > 300:
            health_home = self.config.state_root / ("health_" + uuid4().hex)
            alias = next(iter(self.config.models))
            patch, _ = prepare_profile(
                health_home,
                GatewayModel(
                    self.config.gateway_url,
                    alias,
                    **self.config.models[alias],
                ),
                secret,
            )
            snapshot_skills(self.config.skills, health_home)
            supervisor = self.factory(self.config.executable, self.config.workspace_root, health_home, patch)
            try:
                await supervisor.start()
            finally:
                await supervisor.dispose()
                if supervisor.stop_confirmed:
                    shutil.rmtree(health_home)
            self.protocol_checked_at = time.monotonic()
        repository = await asyncio.to_thread(_inspect_repository, str(self.config.workspace_root), {secret})
        return {
            "engine": "dsh",
            "healthy": True,
            "protocol_initialized": True,
            "skill_hashes": skill_hashes,
            "models": sorted(enabled & self.config.models.keys()),
            "repository": repository,
            "mcp_names": sorted(str(x["name"]) for x in self.config.mcp_servers),
            **result,
            **file_channel,
        }

    def _binding(self, data: dict, *, preparing: bool = False, canceling: bool = False) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM bindings WHERE id=?", (data["binding_id"],)).fetchone()
        if not row or row["task_id"] != data["task_id"] or row["worker_id"] != self.worker_id:
            raise ValueError("Task/Worker/绑定不一致")
        if (
            not preparing
            and not (canceling and row["native_id"] is None)
            and (not row["native_id"] or data.get("native_session_id") != row["native_id"])
        ):
            raise ValueError("原生 Session 与绑定不一致")
        return row

    async def prepare(self, data: dict) -> None:
        existing = self.db.execute("SELECT * FROM bindings WHERE id=?", (data["binding_id"],)).fetchone()
        if existing:
            if (existing["task_id"], existing["worker_id"], existing["workspace"], existing["model"]) != (
                data["task_id"],
                self.worker_id,
                data["workspace"],
                data["model"],
            ):
                raise ValueError("不能修改已有 DSH 绑定")
            if existing["native_id"] is None:
                raise RuntimeError("创建结果未知，禁止自动另建 Session；请人工核对原生磁盘")
            return
        workspace = Path(data["workspace"])
        root = self.config.workspace_root
        # 与现有 Git worktree 规则一致，不能指定任意本机目录。
        from app.worker_agent.workspace import _require_allowed, _validate_registered_worktree

        _require_allowed(root, workspace, root)
        _validate_registered_worktree(root, workspace)
        if workspace.is_symlink() or workspace.resolve(strict=True) == root.resolve():
            raise ValueError("工作区不属于 Worker 的任务 worktree 根目录")
        model = data["model"]
        if model not in self.config.models:
            raise ValueError("模型未在此 Worker 的 Gateway 白名单登记")
        key = self.config.key_file
        if key.is_symlink() or key.stat().st_mode & 0o077 or key.stat().st_uid != os.getuid():
            raise ValueError("Gateway 调用密钥必须是当前用户的私有文件")
        if set(data.get("skill_refs", [])) != set(self.config.skills):
            raise ValueError("Skill 白名单与本 Worker 固定装配不一致")
        if set(data.get("mcp_names", [])) != {str(x["name"]) for x in self.config.mcp_servers}:
            raise ValueError("MCP 白名单与本 Worker 固定装配不一致")
        home = self.config.state_root / data["binding_id"]
        _, profile_hash = prepare_profile(
            home,
            GatewayModel(
                self.config.gateway_url,
                model,
                **self.config.models[model],
            ),
            key.read_text().strip(),
        )
        skill_hashes = snapshot_skills(self.config.skills, home)
        assembly = fingerprint(
            {
                "profile": profile_hash,
                "skills": skill_hashes,
                "mcp": self.config.mcp_servers,
                "dsh": DSH_VERSION,
                "acp": ACP_VERSION,
            }
        )
        self.db.execute(
            "INSERT INTO bindings(id,task_id,worker_id,workspace,model,profile_hash,assembly_hash,state) "
            "VALUES(?,?,?,?,?,?,?,'preparing')",
            (
                data["binding_id"],
                data["task_id"],
                self.worker_id,
                str(workspace.resolve()),
                model,
                profile_hash,
                assembly,
            ),
        )
        self.db.commit()
        host = await self._host(
            self.db.execute("SELECT * FROM bindings WHERE id=?", (data["binding_id"],)).fetchone()
        )
        self.db.execute(
            "UPDATE bindings SET native_id=?,state='prepared' WHERE id=?",
            (host.session_id, data["binding_id"]),
        )
        self.db.commit()

    async def _host(self, row) -> AcpProcessSupervisor:
        binding_id = row["id"]
        if binding_id in self.hosts:
            host = self.hosts[binding_id]
            if host.process and host.process.returncode is None:
                return host
            await host.dispose()
            self.hosts.pop(binding_id)
        if row["owner"]:
            self._previous_process(row, terminate=False)
        home = self.config.state_root / binding_id
        patch = home / "nexus-gateway.patch.yml"
        import yaml

        if (
            hashlib.sha256(json.dumps(yaml.safe_load(patch.read_text()), sort_keys=True).encode()).hexdigest()
            != row["profile_hash"]
        ):
            raise RuntimeError("Profile 完整性校验失败")
        assembly = fingerprint(
            {
                "profile": row["profile_hash"],
                "skills": frozen_manifest(home),
                "mcp": self.config.mcp_servers,
                "dsh": DSH_VERSION,
                "acp": ACP_VERSION,
            }
        )
        if assembly != row["assembly_hash"]:
            raise RuntimeError("DSH/ACP/MCP 装配已变化，拒绝跨配置恢复")

        async def update(session_id, event):
            state = self.db.execute(
                "SELECT state,submission_id FROM bindings WHERE id=?", (binding_id,),
            ).fetchone()
            if not state or state[0] != "running":
                return  # resume 回放不作为当前轮在线增量重新显示。
            self.sequences[binding_id] = self.sequences.get(binding_id, 0) + 1
            # 超大在线块不进入内存队列；明确标记缺口，完整内容从原生分页读取。
            oversized = len(json.dumps(event).encode()) > 65536
            if oversized:
                event = {"sessionUpdate": event.get("sessionUpdate"), "truncated": True}
            self.events.setdefault(binding_id, deque(maxlen=64)).append(
                {
                    "seq": self.sequences[binding_id],
                    "native_session_id": session_id,
                    "submission_id": state[1],
                    "kind": event.get("sessionUpdate"),
                    "payload": event,
                }
            )

        async def permission(session_id, options, call):
            state = self.db.execute("SELECT state FROM bindings WHERE id=?", (binding_id,)).fetchone()
            if not self.connected or not state or state[0] != "running":
                return None
            request_id = uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.permissions[request_id] = {
                "binding_id": binding_id,
                "native_session_id": session_id,
                "options": options,
                "tool_call": call,
                "future": future,
            }
            try:
                return await future
            finally:
                self.permissions.pop(request_id, None)

        host = self.factory(
            self.config.executable,
            Path(row["workspace"]),
            home,
            patch,
            update_sink=update,
            permission_sink=permission,
            permission_timeout=120,
        )
        from app.worker_agent.dsh_acp.preflight import inspect_installation

        await asyncio.to_thread(inspect_installation, self.config.executable, self.config.lock_file)
        await host.start()
        self.hosts[binding_id] = host
        process = psutil.Process(host.process.pid)
        owner = {"pid": process.pid, "created": process.create_time(), "cwd": process.cwd()}
        self.db.execute("UPDATE bindings SET owner=? WHERE id=?", (json.dumps(owner), binding_id))
        self.db.commit()
        await host.create_or_resume(session_id=row["native_id"], mcp_servers=list(self.config.mcp_servers))
        return host

    async def submit(self, row, data: dict, *, gate=lambda: None) -> None:
        sid, text = str(data.get("submission_id", "")), data.get("text")
        fence, generation = data.get("fence"), data.get("generation")
        if (
            not _ID.fullmatch(sid)
            or not isinstance(text, str)
            or not text.strip()
            or type(fence) is not int
            or type(generation) is not int
            or generation < 1
        ):
            raise ValueError("提交缺少内容或明确的 submission/fence/generation")
        digest = fingerprint({"text": text, "fence": fence, "generation": generation})
        receipt = self.db.execute(
            "SELECT * FROM receipts WHERE binding_id=? AND submission_id=?", (row["id"], sid)
        ).fetchone()
        if receipt:
            if receipt["payload_hash"] != digest:
                raise ValueError("同一 submission_id 内容发生变化")
            return  # 包括 unknown，永不重播。
        if fence <= row["fence"] or row["state"] in {"running", "unknown", "preparing", "disposing"}:
            raise RuntimeError("提交 fence 过期或已有执行尚未对账")
        host = await self._host(row)
        gate()
        # 必须在调用 prompt 前落盘，窗口内进程崩溃也只能变成 unknown。
        with self.db:
            self.db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?,'running')",
                (row["id"], sid, fence, generation, digest),
            )
            self.db.execute(
                "UPDATE bindings SET fence=?,state='running',submission_id=?,stop_reason=NULL WHERE id=?",
                (fence, sid, row["id"]),
            )
        prompt = host.submit(text)
        task = asyncio.create_task(self._settle(row["id"], sid, prompt))
        self.turns[row["id"]] = task

    async def _settle(self, binding_id: str, sid: str, prompt: asyncio.Task) -> None:
        try:
            response = await prompt
            reason = response.stop_reason
            state = "succeeded" if reason == "end_turn" else "interrupted"
        except BaseException:
            state, reason = "unknown", None
        with self.db:
            self.db.execute(
                "UPDATE receipts SET state=? WHERE binding_id=? AND submission_id=?", (state, binding_id, sid)
            )
            self.db.execute(
                "UPDATE bindings SET state=?,stop_reason=? "
                "WHERE id=? AND submission_id=? AND state='running'",
                (state, reason, binding_id, sid),
            )

    def _previous_process(self, row, *, terminate: bool) -> None:
        if not row["owner"]:
            return
        owner = json.loads(row["owner"])
        try:
            proc = psutil.Process(owner["pid"])
            if proc.create_time() != owner["created"]:
                try:
                    os.killpg(owner["pid"], 0)
                except ProcessLookupError:
                    return
                raise RuntimeError("PID 已复用且进程组仍存在，无法确认旧所有者已停止")
            if (
                proc.cwd() != owner["cwd"]
                or proc.cwd() != row["workspace"]
                or os.getpgid(proc.pid) != proc.pid
            ):
                raise RuntimeError("原进程所有权无法核实，禁止恢复或释放")
            if not terminate:
                raise RuntimeError("原 DSH Host 仍存活，必须先显式停止")
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            # 组内子进程也必须消失；不能只依据父进程退出释放 Worker。
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                return
            raise RuntimeError("原进程组仍有子进程，尚不能确认停止")
        except psutil.NoSuchProcess:
            # 父进程消失也可能有孤儿，保留组检查。
            try:
                os.killpg(owner["pid"], 0)
            except ProcessLookupError:
                return
            raise RuntimeError("原进程组仍存在，需人工核对") from None

    async def cancel(self, row, data: dict, *, preserve_success: bool = False) -> None:
        fence = data.get("fence")
        if type(fence) is not int or fence < row["fence"]:
            raise ValueError("取消 fence 过期")
        # tombstone 先于取消；阻断所有已经出发但晚到的旧提交。
        with self.db:
            self.db.execute(
                "UPDATE bindings SET fence=?,state=? WHERE id=?",
                (fence, "disposing" if preserve_success else row["state"], row["id"]),
            )
        host = self.hosts.get(row["id"])
        if host:
            result = await host.cancel() if host.session_id else {"confirmed": True}
            if not result.get("confirmed"):
                raise RuntimeError("原生执行尚未确认停止")
            # 完整 dispose 阻止自主后台活动与工具子进程继续写入。
            await host.dispose()
            if not host.stop_confirmed:
                raise RuntimeError("进程组尚未退出")
            self.hosts.pop(row["id"], None)
        else:
            await asyncio.to_thread(self._previous_process, row, terminate=True)
        if turn := self.turns.pop(row["id"], None):
            await asyncio.gather(turn, return_exceptions=True)
        with self.db:
            self.db.execute(
                "UPDATE bindings SET state=?,stop_reason=?,owner=NULL WHERE id=?",
                (
                    "succeeded" if preserve_success else "stopped",
                    row["stop_reason"] if preserve_success else "cancelled",
                    row["id"],
                ),
            )
            self.db.execute(
                "UPDATE receipts SET state='stopped' WHERE binding_id=? AND state IN ('running','unknown')",
                (row["id"],),
            )

    def reply_permission(self, row, data: dict) -> None:
        request = self.permissions.get(data.get("permission_id"))
        if (
            not request
            or request["binding_id"] != row["id"]
            or request["native_session_id"] != row["native_id"]
            or request["future"].done()
        ):
            raise ValueError("审批已过期或不属于当前 Session")
        allowed = {x["optionId"] for x in request["options"] if x["kind"] in {"allow_once", "reject_once"}}
        if data.get("option_id") not in allowed:
            raise ValueError("只支持本次允许或本次拒绝")
        request["future"].set_result(data["option_id"])

    def snapshot(self, row, after: int = 0) -> dict:
        events = list(self.events.get(row["id"], []))
        permissions = [
            {"id": key, **{k: v for k, v in value.items() if k != "future"}}
            for key, value in self.permissions.items()
            if value["binding_id"] == row["id"]
        ]
        return {
            "binding_id": row["id"],
            "task_id": row["task_id"],
            "native_session_id": row["native_id"],
            "worker_id": row["worker_id"],
            "runtime_engine": "dsh",
            "state": row["state"],
            "fence": row["fence"],
            "submission_id": row["submission_id"],
            "stop_reason": row["stop_reason"],
            "dsh_version": DSH_VERSION,
            "acp_version": ACP_VERSION,
            "profile_hash": row["profile_hash"],
            "assembly_hash": row["assembly_hash"],
            "permissions": permissions,
            "events": [event for event in events if event["seq"] > after],
            "event_cursor": self.sequences.get(row["id"], 0),
            "event_gap": bool(
                events
                and (after < events[0]["seq"] - 1 or any(e["payload"].get("truncated") for e in events))
            ),
            "stream_id": self.stream_id,
            "stop_confirmed": (
                row["state"] == "stopped" or (row["state"] == "succeeded" and row["owner"] is None)
            ),
        }

    def connection_lost(self) -> None:
        self.connected = False
        for value in self.permissions.values():
            if not value["future"].done():
                value["future"].set_result(None)

    async def dispose(self) -> None:
        self.connection_lost()
        if self.operations:
            await asyncio.gather(*self.operations, return_exceptions=True)
        for host in self.hosts.values():
            await host.dispose()
        if self.turns:
            await asyncio.gather(*self.turns.values(), return_exceptions=True)
        self.db.close()
        os.close(self._lock)
