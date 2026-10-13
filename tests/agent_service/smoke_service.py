#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实服务进程冒烟测试。

不使用 mock、不冒充真实联调：通过交付的 ``scripts/services.py`` 实际
启动/停止/重启 AgentScope 服务进程，并用真实 HTTP 验证：

1. 原生 Agent CRUD、Session 创建、同员工双 Session 真实物理目录隔离
   （A 写文件 B 经官方 workspace 文件接口不可读）、服务进程重启后同 ID
   恢复且文件仍在（不注入模型密钥）。
2. 服务端模型凭据引导：注入占位密钥时真实持久化一条凭据，重启后仍为
   同一条（幂等），且扩展接口不泄露密钥。

默认不注入真实模型密钥，因此不声称模型推理通过；真实模型联调属后续
任务。证据（脱敏 JSON）写入 ``evidence/phase2/``。

运行：

    uv run python tests/agent_service/smoke_service.py
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

from nexus_agent_service.workspace import SessionWorkspaceManager

ROOT = Path(__file__).resolve().parents[2]
# 隔离的临时 data_dir：绝不删除默认 data/，避免影响本机已有数据。
SMOKE_DATA_DIR = Path(tempfile.mkdtemp(prefix="nexus-smoke-"))
EVIDENCE_DIR = ROOT / "evidence" / "phase2"
USER_ID = "nexus-service"
DEFAULT_CREDENTIAL_ID = "nexus-default-deepseek-credential"
START_TIMEOUT = 60.0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SmokeRunner:
    """用交付脚本启动/停止一个真实服务进程。"""

    def __init__(self, model_api_key: str = "") -> None:
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ)
        self.env.update(
            {
                "AGENTSCOPE_SERVICE_HOST": "127.0.0.1",
                "AGENTSCOPE_SERVICE_PORT": str(self.port),
                "AGENTSCOPE_DATA_DIR": str(SMOKE_DATA_DIR),
                "AGENTSCOPE_MODEL_API_KEY": model_api_key,
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
        # 严格校验退出码：start/stop 必须真实成功（0）。容忍 1 会把
        # 停止失败/未就绪当成成功，可能读到旧进程却误判为已重启。
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


ISOLATION_SENTINEL_NAME = "isolation-sentinel.txt"
ISOLATION_SENTINEL = b"session-a-physical-isolation-sentinel"


async def _physical_isolation_probe(
    basedir: Path,
    user_id: str,
    agent_id: str,
    sid_a: str,
    sid_b: str,
    wsid_a: str,
    wsid_b: str,
) -> tuple[str, str]:
    """用真实公开扩展确认真实物理目录隔离，并在 A 写文件。

    返回 (A 物理目录, B 物理目录)。B 目录中不应存在 A 的文件。
    """
    manager = SessionWorkspaceManager(basedir=str(basedir))
    try:
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
        assert workspace_a.workdir != workspace_b.workdir, (
            workspace_a.workdir,
            workspace_b.workdir,
        )
        backend_a = workspace_a.get_backend()
        await backend_a.write_file(
            os.path.join(workspace_a.workdir, ISOLATION_SENTINEL_NAME),
            ISOLATION_SENTINEL,
        )
        assert await backend_a.file_exists(
            os.path.join(workspace_a.workdir, ISOLATION_SENTINEL_NAME),
        )
        assert not await workspace_b.get_backend().file_exists(
            os.path.join(workspace_b.workdir, ISOLATION_SENTINEL_NAME),
        )
        return workspace_a.workdir, workspace_b.workdir
    finally:
        await manager.close_all()


def scenario_crud() -> dict:
    """无密钥：原生 CRUD/Session/隔离/重启恢复。"""
    runner = SmokeRunner(model_api_key="")
    steps: list[dict] = []

    def record(step: str, ok: bool, **details) -> None:
        entry = {"step": step, "ok": ok}
        entry.update(details)
        steps.append(entry)
        print(f"[{'OK' if ok else 'FAIL'}] {step} {details or ''}")

    evidence: dict = {"scenario": "crud_no_model_key", "steps": steps}
    agent_id: str | None = None
    session_ids: list[str] = []
    client = httpx.Client(base_url=runner.base_url, timeout=30.0)
    try:
        runner.start()
        record("start", True, port=runner.port)

        spec = client.get("/openapi.json").json()
        evidence["openapi"] = {
            "version": spec["info"]["version"],
            "path_count": len(spec["paths"]),
        }
        record(
            "openapi_contract",
            True,
            version=spec["info"]["version"],
            paths=len(spec["paths"]),
        )

        health = client.get("/nexus/health").json()
        assert health["model_configured"] is False
        defaults = client.get("/nexus/defaults").json()
        assert defaults["model_configured"] is False
        assert defaults["chat_model_config"] is None
        evidence["model_configured"] = False
        record("model_unconfigured_reported", True)

        create = client.post(
            "/agent/",
            headers=headers(),
            json={
                "name": "冒烟员工",
                "system_prompt": "你是冒烟测试员工。",
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

        listing = client.get("/agent/", headers=headers()).json()
        matched = next(a for a in listing["agents"] if a["id"] == agent_id)
        two_ids_differ = matched["data"]["id"] != matched["id"]
        assert two_ids_differ
        evidence["agent"] = {
            "id": matched["id"],
            "data_id": matched["data"]["id"],
            "two_ids_differ": two_ids_differ,
        }
        record("agent_top_level_id", True, two_ids_differ=two_ids_differ)

        client.patch(
            f"/agent/{agent_id}",
            headers=headers(),
            json={"name": "冒烟员工-已改名"},
        ).raise_for_status()
        renamed = next(
            a
            for a in client.get("/agent/", headers=headers()).json()["agents"]
            if a["id"] == agent_id
        )
        assert renamed["data"]["name"] == "冒烟员工-已改名"
        record("agent_update", True, agent_name=renamed["data"]["name"])

        for _ in range(2):
            created = client.post(
                "/sessions/",
                headers=headers(),
                json={"agent_id": agent_id, "name": "冒烟对话"},
            )
            created.raise_for_status()
            session_ids.append(created.json()["session_id"])
        record("session_create", True, session_ids=session_ids)

        views = client.get(
            "/sessions/",
            headers=headers(),
            params={"agent_id": agent_id},
        ).json()["sessions"]
        wanted = [v for v in views if v["session"]["id"] in session_ids]
        assert len(wanted) == 2
        # session id → 原生持久 workspace_id
        workspace_ids = {
            v["session"]["id"]: v["session"]["config"]["workspace_id"]
            for v in wanted
        }
        sid_a, sid_b = session_ids
        wsid_a, wsid_b = workspace_ids[sid_a], workspace_ids[sid_b]
        # ID 不同只是必要条件；以下核对真实物理目录与文件可见性。
        assert wsid_a != wsid_b
        dir_a, dir_b = asyncio.run(
            _physical_isolation_probe(
                SMOKE_DATA_DIR / "workspaces",
                USER_ID,
                agent_id,
                sid_a,
                sid_b,
                wsid_a,
                wsid_b,
            ),
        )
        assert dir_a != dir_b
        # B 经官方 /workspace/files 不可读 A 的文件，A 可读。
        read_b = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_b,
                "path": ISOLATION_SENTINEL_NAME,
            },
        )
        assert read_b.status_code == 404, read_b.text
        read_a = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_a,
                "path": ISOLATION_SENTINEL_NAME,
            },
        )
        assert read_a.status_code == 200, read_a.text
        assert read_a.content == ISOLATION_SENTINEL
        evidence["physical_isolation"] = {
            "session_a": {"id": sid_a, "workspace_id": wsid_a, "workdir": dir_a},
            "session_b": {"id": sid_b, "workspace_id": wsid_b, "workdir": dir_b},
            "workdirs_differ": dir_a != dir_b,
            "b_read_status": read_b.status_code,
            "a_read_status": read_a.status_code,
        }
        record(
            "per_session_physical_isolation",
            True,
            workdirs_differ=dir_a != dir_b,
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
            "changed": generation_after != generation_before,
        }
        record("restart_new_generation", True)

        restored = next(
            (
                a
                for a in client.get("/agent/", headers=headers()).json()[
                    "agents"
                ]
                if a["id"] == agent_id
            ),
            None,
        )
        assert restored is not None
        restored_ids = {
            v["session"]["id"]
            for v in client.get(
                "/sessions/",
                headers=headers(),
                params={"agent_id": agent_id},
            ).json()["sessions"]
            if v["session"]["id"] in session_ids
        }
        assert restored_ids == set(session_ids)
        evidence["restart_recovery"] = {
            "agent_id": agent_id,
            "name": restored["data"]["name"],
            "session_ids": sorted(restored_ids),
        }
        record("restart_recovery", True)

        # 真实进程重启后：同一 workspace_id 重建同一目录，A 文件仍在，
        # B 仍不可读（物理隔离跨重启保持）。
        read_a_restart = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_a,
                "path": ISOLATION_SENTINEL_NAME,
            },
        )
        assert read_a_restart.status_code == 200, read_a_restart.text
        assert read_a_restart.content == ISOLATION_SENTINEL
        read_b_restart = client.get(
            "/workspace/files",
            headers=headers(),
            params={
                "agent_id": agent_id,
                "session_id": sid_b,
                "path": ISOLATION_SENTINEL_NAME,
            },
        )
        assert read_b_restart.status_code == 404, read_b_restart.text
        evidence["restart_workspace_persistence"] = {
            "session_a_read_status": read_a_restart.status_code,
            "session_b_read_status": read_b_restart.status_code,
        }
        record("restart_workspace_persistence", True)

        deleted = client.delete(f"/agent/{agent_id}", headers=headers())
        assert deleted.status_code == 204
        after_delete = client.get("/agent/", headers=headers()).json()
        assert all(a["id"] != agent_id for a in after_delete["agents"])
        evidence["agent_deleted"] = True
        record("agent_delete", True)

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        record("error", False, error=str(exc))
        print(f"冒烟失败：{exc}", file=sys.stderr)
    finally:
        client.close()
        runner.stop_best_effort()
    return evidence


def scenario_model_bootstrap() -> dict:
    """占位密钥：真实持久化凭据且重启幂等，扩展接口不泄露密钥。"""
    secret = "smoke-placeholder-not-a-real-key"
    runner = SmokeRunner(model_api_key=secret)
    steps: list[dict] = []

    def record(step: str, ok: bool, **details) -> None:
        entry = {"step": step, "ok": ok}
        entry.update(details)
        steps.append(entry)
        print(f"[{'OK' if ok else 'FAIL'}] {step} {details or ''}")

    evidence: dict = {"scenario": "model_bootstrap_placeholder_key", "steps": steps}
    client = httpx.Client(base_url=runner.base_url, timeout=30.0)

    def credential_ids() -> list[str]:
        body = client.get("/credential/", headers=headers()).json()
        creds = [
            c
            for c in body["credentials"]
            if c.get("data", {}).get("type") == "deepseek_credential"
        ]
        return [c["id"] for c in creds]

    try:
        runner.start()
        record("start", True, port=runner.port)

        health = client.get("/nexus/health").json()
        assert health["model_configured"] is True
        defaults = client.get("/nexus/defaults").json()
        assert defaults["model_configured"] is True
        cfg = defaults["chat_model_config"]
        assert cfg["credential_id"] == DEFAULT_CREDENTIAL_ID
        assert cfg["type"] == "deepseek_chat"
        # 扩展接口绝不返回密钥。
        assert secret not in client.get("/nexus/defaults").text
        assert secret not in client.get("/nexus/health").text
        evidence["default_chat_model_config"] = {
            "credential_id": cfg["credential_id"],
            "type": cfg["type"],
            "model": cfg["model"],
            "parameters": cfg["parameters"],
        }
        record("defaults_with_model_configured", True)

        first_ids = credential_ids()
        assert first_ids == [DEFAULT_CREDENTIAL_ID], first_ids
        record("credential_persisted_once", True, credential_id=first_ids[0])

        generation_before = generation_id(client)
        runner.stop()
        runner.start()
        generation_after = generation_id(client)
        assert generation_after != generation_before, (
            generation_before,
            generation_after,
        )
        second_ids = credential_ids()
        assert second_ids == [DEFAULT_CREDENTIAL_ID], second_ids
        evidence["restart_idempotent_credential"] = {
            "count": len(second_ids),
            "credential_id": second_ids[0],
            "generation_before": generation_before,
            "generation_after": generation_after,
            "generation_changed": True,
        }
        record("credential_idempotent_after_restart", True)

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        record("error", False, error=str(exc))
        print(f"模型引导冒烟失败：{exc}", file=sys.stderr)
    finally:
        client.close()
        runner.stop_best_effort()
    return evidence


def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    evidence = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "service": "agentscope",
        "data_dir": str(SMOKE_DATA_DIR),
        "crud": scenario_crud(),
        "model_bootstrap": scenario_model_bootstrap(),
        "limitations": [
            "未注入真实模型密钥：仅验证原生 CRUD/Session/物理目录隔离/重启"
            "恢复与凭据引导，不声称模型推理通过。",
            "文件隔离验证只覆盖本脚本自己的隔离临时 data_dir 与单进程部署。",
            "单进程 InMemoryMessageBus：未验证多进程事件总线恢复。",
            "未验证多轮流式聊天、附件、Knowledge 与员工删除归档。",
        ],
    }
    evidence["status"] = (
        "passed"
        if evidence["crud"]["status"] == "passed"
        and evidence["model_bootstrap"]["status"] == "passed"
        else "failed"
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-{stamp}.json"
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    print(f"证据已写入 {out}")
    shutil.rmtree(SMOKE_DATA_DIR, ignore_errors=True)
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
