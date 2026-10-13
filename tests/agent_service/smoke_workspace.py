#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实进程 + 官方 workspace 文件 API 的物理隔离冒烟测试。

不使用 mock：通过交付的 ``scripts/services.py`` 实际 start/stop/start 真实
AgentScope 服务进程，用真实 HTTP 创建原生 Agent 与两个同员工 Session，从
Session 配置取得原生持久 ``workspace_id``，然后验证：

1. 同员工双 Session 的 workspace ID 不同，且实际物理目录不同；
2. 在 A 的 workspace 真实写入文件；B 经官方 ``/workspace/files`` 不可读
   （404），A 可读（200）；
3. 真实进程停止并重新启动后，A 同一 workspace_id 重建同一目录、文件仍在，
   B 仍不可读；
4. 恶意/异常 ``workspace_id`` 不造成路径穿越；
5. 并发首次请求同一 ``workspace_id`` 返回同一个 workspace；
6. ``close`` / ``close_all`` 正常释放缓存。

写在 ``agent-service/nexus_agent_service/workspace.py`` 的
``SessionWorkspaceManager`` 是交付服务的同一公开扩展实现。文件写入只发生
在本脚本自己的隔离临时 data_dir，绝不触碰默认 ``data/``，不读取 Home 或
凭据，不调用模型，因此不声称推理验证。证据（脱敏 JSON）写入
``evidence/phase3/``。

运行：

    uv run python tests/agent_service/smoke_workspace.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from nexus_agent_service.workspace import (
    SessionWorkspaceManager,
    workspace_dir_name,
)

ROOT = Path(__file__).resolve().parents[2]
# 隔离的临时 data_dir：绝不删除默认 data/，避免影响本机已有数据。
SMOKE_DATA_DIR = Path(tempfile.mkdtemp(prefix="nexus-ws-smoke-"))
EVIDENCE_DIR = ROOT / "evidence" / "phase3"
USER_ID = "nexus-service"
START_TIMEOUT = 60.0
SENTINEL_NAME = "physical-isolation-sentinel.txt"
SENTINEL = b"session-a-physical-isolation-sentinel"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SmokeRunner:
    """用交付脚本启动/停止一个真实服务进程。"""

    def __init__(self) -> None:
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ)
        self.env.update(
            {
                "AGENTSCOPE_SERVICE_HOST": "127.0.0.1",
                "AGENTSCOPE_SERVICE_PORT": str(self.port),
                "AGENTSCOPE_DATA_DIR": str(SMOKE_DATA_DIR),
                # 不注入模型密钥：本冒烟不验证模型。
                "AGENTSCOPE_MODEL_API_KEY": "",
                "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
            },
        )

    def _run_services(self, action: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [sys.executable, "scripts/services.py", action, "agentscope"],
            cwd=str(ROOT),
            env=self.env,
            capture_output=True,
            text=True,
        )
        # 严格校验退出码：start/stop 必须真实成功（0）。容忍非 0 会把停止
        # 失败或未就绪当成成功，可能读到旧健康进程却误判为已重启。
        if result.returncode != 0:
            raise RuntimeError(
                f"services.py {action} 退出码 {result.returncode}: "
                f"{result.stderr.strip()}",
            )
        return result

    def start(self) -> None:
        self._run_services("start")
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{self.base_url}/nexus/health", timeout=2.0)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("服务启动后健康检查超时")

    def stop(self) -> None:
        self._run_services("stop")

    def stop_best_effort(self) -> None:
        """清理用停止：失败只报告，不掩盖场景原始结果。"""
        try:
            self._run_services("stop")
        except Exception as exc:  # noqa: BLE001
            print(f"清理停止失败：{exc}", file=sys.stderr)


def headers() -> dict:
    return {"X-User-ID": USER_ID}


def generation_id(client: httpx.Client) -> str:
    """读取当前服务代次；缺失即视为失败（不能证明真实重启）。"""
    health = client.get("/nexus/health").json()
    gen = health.get("generation_id")
    assert gen, "健康接口未返回 generation_id"
    return gen


async def _in_process_checks(
    basedir: Path,
    agent_id: str,
    sid_a: str,
    sid_b: str,
    wsid_a: str,
    wsid_b: str,
) -> dict:
    """在进程内用交付的公开扩展做隔离/穿越/并发/生命周期核对。"""
    manager = SessionWorkspaceManager(basedir=str(basedir))
    result: dict = {}
    try:
        workspace_a = await manager.get_workspace(
            USER_ID,
            agent_id,
            sid_a,
            wsid_a,
        )
        workspace_b = await manager.get_workspace(
            USER_ID,
            agent_id,
            sid_b,
            wsid_b,
        )
        result["workdir_a"] = workspace_a.workdir
        result["workdir_b"] = workspace_b.workdir
        result["workdirs_differ"] = workspace_a.workdir != workspace_b.workdir
        result["workdir_a_matches_id"] = (
            workspace_a.workdir == manager.workdir_for(wsid_a)
        )

        backend_a = workspace_a.get_backend()
        await backend_a.write_file(
            os.path.join(workspace_a.workdir, SENTINEL_NAME),
            SENTINEL,
        )
        result["a_file_written"] = await backend_a.file_exists(
            os.path.join(workspace_a.workdir, SENTINEL_NAME),
        )
        result["b_file_absent"] = not await workspace_b.get_backend().file_exists(
            os.path.join(workspace_b.workdir, SENTINEL_NAME),
        )

        # 路径穿越：恶意 ID 只映射为基于根目录的哈希目录。
        basedir_real = os.path.realpath(str(basedir))
        traversal_ok = True
        for malicious in ("../../etc/passwd", "..", "/tmp/escape", "a/b\\c"):
            ws = await manager.get_workspace(
                USER_ID,
                "trav-agent",
                "trav-session",
                malicious,
            )
            resolved = os.path.realpath(ws.workdir)
            name = os.path.basename(ws.workdir)
            if not resolved.startswith(basedir_real + os.sep):
                traversal_ok = False
            if name != workspace_dir_name(malicious):
                traversal_ok = False
        result["path_traversal_contained"] = traversal_ok

        # 并发同一 ID 返回同一实例。
        concurrent = await asyncio.gather(
            *(
                manager.get_workspace(
                    USER_ID,
                    agent_id,
                    sid_a,
                    wsid_a,
                )
                for _ in range(8)
            ),
        )
        result["concurrent_same_instance"] = all(
            item is concurrent[0] for item in concurrent
        )

        # close 释放后重建，close_all 清空缓存。
        await manager.close(wsid_a)
        recreated = await manager.get_workspace(
            USER_ID,
            agent_id,
            sid_a,
            wsid_a,
        )
        result["close_then_reinit_same_workdir"] = (
            recreated.workdir == result["workdir_a"]
        )
        await manager.close_all()
        result["close_all_clears_cache"] = manager._workspaces == {}  # noqa: SLF001
    finally:
        await manager.close_all()
    return result


def scenario_physical_isolation() -> dict:
    runner = SmokeRunner()
    steps: list[dict] = []

    def record(step: str, ok: bool, **details) -> None:
        entry = {"step": step, "ok": ok}
        entry.update(details)
        steps.append(entry)
        print(f"[{'OK' if ok else 'FAIL'}] {step} {details or ''}")

    evidence: dict = {"scenario": "physical_workspace_isolation", "steps": steps}
    client = httpx.Client(base_url=runner.base_url, timeout=30.0)
    try:
        runner.start()
        record("start", True, port=runner.port)

        create = client.post(
            "/agent/",
            headers=headers(),
            json={
                "name": "物理隔离员工",
                "system_prompt": "你是物理隔离冒烟员工。",
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
        record("agent_create", True, agent_id=agent_id)

        session_ids: list[str] = []
        for _ in range(2):
            created = client.post(
                "/sessions/",
                headers=headers(),
                json={"agent_id": agent_id, "name": "隔离对话"},
            )
            created.raise_for_status()
            session_ids.append(created.json()["session_id"])
        sid_a, sid_b = session_ids
        record("session_create", True, session_ids=session_ids)

        views = client.get(
            "/sessions/",
            headers=headers(),
            params={"agent_id": agent_id},
        ).json()["sessions"]
        wanted = [v for v in views if v["session"]["id"] in session_ids]
        assert len(wanted) == 2
        workspace_ids = {
            v["session"]["id"]: v["session"]["config"]["workspace_id"]
            for v in wanted
        }
        wsid_a, wsid_b = workspace_ids[sid_a], workspace_ids[sid_b]
        assert wsid_a != wsid_b
        record("workspace_ids_differ", True, wsid_a=wsid_a, wsid_b=wsid_b)

        checks = asyncio.run(
            _in_process_checks(
                SMOKE_DATA_DIR / "workspaces",
                agent_id,
                sid_a,
                sid_b,
                wsid_a,
                wsid_b,
            ),
        )
        assert checks["workdirs_differ"]
        assert checks["workdir_a_matches_id"]
        assert checks["a_file_written"]
        assert checks["b_file_absent"]
        assert checks["path_traversal_contained"]
        assert checks["concurrent_same_instance"]
        assert checks["close_then_reinit_same_workdir"]
        assert checks["close_all_clears_cache"]
        evidence["in_process_checks"] = checks
        record(
            "in_process_physical_isolation",
            True,
            workdirs_differ=checks["workdirs_differ"],
            traversal=checks["path_traversal_contained"],
            concurrent=checks["concurrent_same_instance"],
            lifecycle=checks["close_all_clears_cache"],
        )

        # 官方 /workspace/files：A 可读，B 不可读。
        read_a = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_a,
                "path": SENTINEL_NAME,
            },
        )
        assert read_a.status_code == 200, read_a.text
        assert read_a.content == SENTINEL
        read_b = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_b,
                "path": SENTINEL_NAME,
            },
        )
        assert read_b.status_code == 404, read_b.text
        record(
            "official_workspace_file_read",
            True,
            a_status=read_a.status_code,
            b_status=read_b.status_code,
        )

        generation_before = generation_id(client)
        runner.stop()
        record("stop_before_restart", True)
        runner.start()
        generation_after = generation_id(client)
        # 真实重启必须产生新的服务代次；否则说明读到的仍是旧进程。
        assert generation_after != generation_before, (
            generation_before,
            generation_after,
        )
        evidence["restart_generation"] = {
            "before": generation_before,
            "after": generation_after,
            "changed": True,
        }
        record(
            "restart_new_generation",
            True,
            before=generation_before,
            after=generation_after,
        )

        # 真实进程重启后同一 workspace_id 重建同一目录、文件仍在，B 仍不可读。
        read_a2 = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_a,
                "path": SENTINEL_NAME,
            },
        )
        assert read_a2.status_code == 200, read_a2.text
        assert read_a2.content == SENTINEL
        read_b2 = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_b,
                "path": SENTINEL_NAME,
            },
        )
        assert read_b2.status_code == 404, read_b2.text
        evidence["restart_persistence"] = {
            "session_a_read_status": read_a2.status_code,
            "session_b_read_status": read_b2.status_code,
        }
        record(
            "restart_persistence_after_process_restart",
            True,
            a_status=read_a2.status_code,
            b_status=read_b2.status_code,
        )

        deleted = client.delete(f"/agent/{agent_id}", headers=headers())
        assert deleted.status_code == 204
        record("agent_delete", True)

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        record("error", False, error=str(exc))
        print(f"物理隔离冒烟失败：{exc}", file=sys.stderr)
    finally:
        client.close()
        runner.stop_best_effort()
    return evidence


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    evidence = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "service": "agentscope",
        "scope": "physical_workspace_isolation",
        "data_dir": str(SMOKE_DATA_DIR),
        "physical_isolation": scenario_physical_isolation(),
        "limitations": [
            "未注入真实模型密钥：只验证物理目录/文件隔离与重启恢复，不声称"
            "模型推理通过。",
            "只覆盖单进程部署与本脚本自己的隔离临时 data_dir。",
            "不验证多进程事件总线恢复、附件、Knowledge 与员工删除归档。",
        ],
    }
    evidence["status"] = evidence["physical_isolation"]["status"]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-workspace-{stamp}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"证据已写入 {out}")
    shutil.rmtree(SMOKE_DATA_DIR, ignore_errors=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
