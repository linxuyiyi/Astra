#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""真实服务进程冒烟测试。

不使用 mock、不冒充真实联调：通过交付的 ``scripts/services.py`` 实际
启动/停止/重启 AgentScope 服务进程，并用真实 HTTP 验证：

1. 原生 Agent CRUD、Session 创建、PER_SESSION workspace 隔离、服务进程
   重启后同 ID 恢复（不注入模型密钥）。
2. 服务端模型凭据引导：注入占位密钥时真实持久化一条凭据，重启后仍为
   同一条（幂等），且扩展接口不泄露密钥。

默认不注入真实模型密钥，因此不声称模型推理通过；真实模型联调属后续
任务。证据（脱敏 JSON）写入 ``evidence/phase2/``。

运行：

    uv run python tests/agent_service/smoke_service.py
"""
from __future__ import annotations

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
        if result.returncode not in (0, 1):
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


def headers() -> dict:
    return {"X-User-ID": USER_ID}


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
        workspace_ids = [
            v["session"]["config"]["workspace_id"] for v in wanted
        ]
        assert len(set(workspace_ids)) == 2
        evidence["sessions"] = [
            {
                "id": v["session"]["id"],
                "workspace_id": v["session"]["config"]["workspace_id"],
            }
            for v in wanted
        ]
        record("per_session_workspace_isolation", True)

        runner.stop()
        record("stop_before_restart", True)
        runner.start()
        record("restart", True)

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
        runner.stop()
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

        runner.stop()
        runner.start()
        second_ids = credential_ids()
        assert second_ids == [DEFAULT_CREDENTIAL_ID], second_ids
        evidence["restart_idempotent_credential"] = {
            "count": len(second_ids),
            "credential_id": second_ids[0],
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
        runner.stop()
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
            "未注入真实模型密钥：仅验证原生 CRUD/Session/隔离/重启恢复与"
            "凭据引导，不声称模型推理通过。",
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
