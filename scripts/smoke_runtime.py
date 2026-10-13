#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nexus 真实运行时联调脚本（OpenSpec 任务 2.2）。

通过交付的 ``scripts/services.py`` **实际启动/停止/重启**两个真实进程
（官方 AgentScope 服务 + Nexus 后端），并用轻量 HTTP 适配器驱动真实模型：

1. 隔离的临时 data_dir/端口，绝不触碰默认 ``data/``；只从环境与仓库根
   ``.env`` 读取配置，不读取用户 Home 凭据，不写入密钥。
2. 通过适配器创建原生测试 Agent 与两个原生 Session；每轮先读取历史游标，
   再 ``POST /chat`` 触发，然后从该游标订阅
   ``GET /sessions/{sid}/stream``（``after=cursor`` 覆盖瞬时 replay），
   收集 ``REPLY_START`` / ``TEXT_BLOCK_DELTA`` / ``REPLY_END``，按事件 id
   与 reply_id 去重；每轮都必须 ``finished_reason == completed`` 且包含
   ``REPLY_START``、非空 ``TEXT_BLOCK_DELTA`` 与 ``REPLY_END``；``REPLY_END``
   后等待 ``/status`` 真正 ``idle``，再读取持久化历史（``before`` 游标分页）
   作为最终内容事实源。
3. 随机口令两轮上下文：第一 Session 记住口令后第二轮必须回忆；第二
   Session 询问同一口令不得拿到第一 Session 的上下文；两 Session 的
   ``PER_SESSION`` workspace 必须不同。
4. 同一原生 Session 在 AgentScope 真实进程 stop/start 后继续第三轮回忆
   口令：验证重启持久化，且**不注入 env 密钥**重启仍能复用已持久化凭据。

需要已配置的模型密钥（``AGENTSCOPE_MODEL_API_KEY``，由调用方安全注入）；
未配置时明确退出非零、标记未验证，**不 fallback 假模型、不伪造回复**。

    uv run python scripts/smoke_runtime.py                  # 完整真实模型联调
    uv run python scripts/smoke_runtime.py --connectivity-only  # 仅真实健康连通

``--keep-evidence`` 保留本次临时数据目录用于排查；默认清理。证据（脱敏，
不含密钥）写入 ``evidence/phase2/``。真实文本含随机口令可记录，密钥绝不
记录。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))
sys.path.insert(0, str(ROOT / "backend"))

from nexus_backend.adapter import AgentScopeHTTPClient, UpstreamError  # noqa: E402

EVIDENCE_DIR = ROOT / "evidence" / "phase2"
USER_ID = "nexus-service"
EXPECTED_VERSION = "2.0.10"
HEALTH_TIMEOUT = 60.0
ROUND_TIMEOUT = 180.0
IDLE_TIMEOUT = 120.0


class SmokeError(RuntimeError):
    """联调断言失败（含未配置），用于区分于意外异常。"""


def _safe_error(exc: BaseException) -> str:
    """证据里的错误文本：已知安全异常保留消息，其他只留类型名。

    绝不把上游响应原文、传输异常 repr 或凭据串写进证据。
    """
    if isinstance(exc, (SmokeError, UpstreamError)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ServiceController:
    """用交付脚本启动/停止真实服务进程，并维护隔离环境。"""

    data_root: Path
    asc_port: int
    backend_port: int
    steps: list[dict] = field(default_factory=list)

    @property
    def asc_url(self) -> str:
        return f"http://127.0.0.1:{self.asc_port}"

    @property
    def backend_url(self) -> str:
        return f"http://127.0.0.1:{self.backend_port}"

    def base_env(self) -> dict:
        env = dict(os.environ)
        env.update(
            {
                "AGENTSCOPE_SERVICE_HOST": "127.0.0.1",
                "AGENTSCOPE_SERVICE_PORT": str(self.asc_port),
                "AGENTSCOPE_DATA_DIR": str(self.data_root / "agentscope"),
                "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
                "AGENTSCOPE_SERVICE_URL": self.asc_url,
                "NEXUS_BACKEND_HOST": "127.0.0.1",
                "NEXUS_BACKEND_PORT": str(self.backend_port),
                "NEXUS_BACKEND_DATA_DIR": str(self.data_root / "backend"),
            },
        )
        return env

    def env_with_key(self, key: str) -> dict:
        env = self.base_env()
        env["AGENTSCOPE_MODEL_API_KEY"] = key
        return env

    def env_without_key(self) -> dict:
        # 显式清空，模拟“重启时不注入 env 密钥”，验证持久凭据复用。
        env = self.base_env()
        env["AGENTSCOPE_MODEL_API_KEY"] = ""
        return env

    def run(self, action: str, service: str, env: dict) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/services.py", action, service],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SmokeError(
                f"services.py {action} {service} 失败"
                f"（exit {result.returncode}）：{result.stderr.strip()}",
            )

    def start(self, service: str, env: dict) -> None:
        self.run("start", service, env)

    def stop(self, service: str, env: dict) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/services.py", "stop", service],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SmokeError(
                f"services.py stop {service} 失败：{result.stderr.strip()}",
            )


async def _wait_health(url: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: str = "无响应"
    async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                body = resp.json()
                if resp.status_code == 200 and body.get("ready") is True:
                    return body
                last = f"HTTP {resp.status_code} {body.get('status')}"
            except httpx.HTTPError as exc:
                last = _safe_error(exc)
            await asyncio.sleep(0.5)
    raise SmokeError(f"健康检查超时（{url}）：{last}")


def _input_msg(text: str, msg_id: str) -> dict:
    return {
        "id": msg_id,
        "name": "人类",
        "role": "user",
        "content": [{"type": "text", "text": text}],
        "metadata": {"smoke_runtime": True},
    }


def _assistant_text(message: dict) -> str:
    parts = [
        block.get("text", "")
        for block in message.get("content", [])
        if block.get("type") == "text"
    ]
    return "".join(parts)


async def _wait_idle(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    session_id: str,
) -> dict:
    deadline = time.monotonic() + IDLE_TIMEOUT
    last = None
    while time.monotonic() < deadline:
        status = await adapter.get_session_status(agent_id, session_id)
        last = status.get("status")
        if last == "idle":
            return status
        await asyncio.sleep(0.5)
    raise SmokeError(f"等待会话 idle 超时，最后状态：{last}")


async def _collect_until_end(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    session_id: str,
    cursor: str | None,
) -> tuple[list[dict], dict]:
    """订阅 SSE 直到 REPLY_END；按事件 id 去重，返回事件与终态事件。"""
    events: list[dict] = []
    seen_event_ids: set[str] = set()
    end_event: dict | None = None
    stream = adapter.stream_events(agent_id, session_id, after=cursor)

    async def consume() -> None:
        nonlocal end_event
        async for event in stream:
            event_id = event.get("id")
            if event_id is not None:
                if event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)
            events.append(event)
            if event.get("type") == "REPLY_END":
                end_event = event
                return

    try:
        await asyncio.wait_for(consume(), timeout=ROUND_TIMEOUT)
    finally:
        await stream.aclose()
    if end_event is None:
        raise SmokeError("SSE 未收到 REPLY_END")
    return events, end_event


async def run_round(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    session_id: str,
    text: str,
) -> dict:
    """一轮：取历史游标→触发→按游标订阅 SSE→等 idle→按历史校准回复。

    先读 ``event_cursor`` 再触发、随后 ``after=cursor`` 订阅，可覆盖从触发
    到订阅之间产生的 replay 事件。每轮都必须真实完成：``finished_reason``
    为 ``completed``，且流内含 ``REPLY_START``、非空 ``TEXT_BLOCK_DELTA``
    与 ``REPLY_END``；否则不得据此判定上下文或会话隔离结论。
    """
    before = await adapter.list_messages(agent_id, session_id)
    cursor = before.get("event_cursor")
    trigger = await adapter.trigger_chat(
        agent_id,
        session_id,
        _input_msg(text, uuid.uuid4().hex),
    )
    if trigger.get("status") != "started":
        raise SmokeError(f"POST /chat 未返回 started：{trigger}")

    events, end_event = await _collect_until_end(
        adapter,
        agent_id,
        session_id,
        cursor,
    )
    event_types = [e.get("type") for e in events]
    reply_id = end_event.get("reply_id")
    finished_reason = end_event.get("finished_reason")
    if finished_reason != "completed":
        raise SmokeError(
            f"本轮未正常完成（finished_reason={finished_reason!r}）",
        )
    if "REPLY_START" not in event_types:
        raise SmokeError("SSE 缺少 REPLY_START")
    if "REPLY_END" not in event_types:
        raise SmokeError("SSE 缺少 REPLY_END")
    has_text_delta = any(
        e.get("type") == "TEXT_BLOCK_DELTA"
        and isinstance(e.get("delta"), str)
        and e["delta"].strip()
        for e in events
    )
    if not has_text_delta:
        raise SmokeError("SSE 缺少非空 TEXT_BLOCK_DELTA")
    await _wait_idle(adapter, agent_id, session_id)

    # 终态后以历史为准（REPLY_END 先于 finally 落盘，故必须 idle 后再读，
    # 并容忍极短的落盘延迟）。
    reply = None
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        messages = await adapter.iter_all_messages(agent_id, session_id)
        reply = next(
            (m for m in messages if m.get("id") == reply_id),
            None,
        )
        if reply is not None:
            break
        await asyncio.sleep(0.5)
    if reply is None:
        raise SmokeError(f"历史中找不到 reply_id={reply_id} 的 assistant 消息")
    reply_text = _assistant_text(reply)
    if not reply_text.strip():
        raise SmokeError("历史校准得到的回复为空，本轮不得视为完成")
    return {
        "prompt": text,
        "reply_id": reply_id,
        "finished_reason": finished_reason,
        "event_types": event_types,
        "event_ids": [e.get("id") for e in events],
        "reply_preview": reply_text[:200],
        "reply_text": reply_text,
    }


async def _connectivity_only(ctrl: ServiceController, args) -> dict:
    """仅验证后端真实健康连通与上游停止后的明确不可用（不调用模型）。"""
    evidence = {
        "mode": "connectivity_only",
        "run_at": _now(),
        "checks": {},
    }
    env = ctrl.env_without_key()
    ctrl.start("agentscope", env)
    ctrl.start("nexus-backend", env)
    try:
        backend_health = await _wait_health(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        assert backend_health["status"] == "ok", backend_health
        upstream = backend_health["agentscope"]
        assert upstream["reachable"] is True, upstream
        assert upstream["version"] == EXPECTED_VERSION, upstream
        assert upstream["version_ok"] is True, upstream
        evidence["backend"] = {
            "base_url": ctrl.backend_url,
            "status": backend_health["status"],
            "instance_id": backend_health.get("instance_id"),
        }
        evidence["agentscope"] = {
            "base_url": ctrl.asc_url,
            "version": upstream["version"],
            "reachable": upstream["reachable"],
            "model_configured": upstream["model_configured"],
        }
        evidence["checks"]["backend_health_ok"] = True
        evidence["checks"]["agentscope_version_ok"] = True

        # 停止真实上游进程：后端健康必须明确报告不可用，不伪造成功。
        ctrl.stop("agentscope", env)
        unavailable = await _wait_unavailable(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        evidence["after_stop"] = {
            "http_status": unavailable["http_status"],
            "status": unavailable["body"].get("status"),
            "reachable": unavailable["body"].get("agentscope", {}).get(
                "reachable",
            ),
            "reason": unavailable["body"].get("reason"),
        }
        assert unavailable["http_status"] == 503, unavailable
        assert unavailable["body"].get("status") == "unavailable", unavailable
        assert (
            unavailable["body"].get("agentscope", {}).get("reachable")
            is False
        ), unavailable
        evidence["checks"]["upstream_stopped_reports_unavailable"] = True
        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        ctrl.stop("nexus-backend", env)
        ctrl.stop("agentscope", env)
    return evidence


async def _wait_unavailable(url: str, timeout: float) -> dict:
    """轮询直到后端健康明确报告上游不可用（HTTP 503, status=unavailable）。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                body = resp.json()
                last = {"http_status": resp.status_code, "body": body}
                if resp.status_code == 503 and body.get("status") == (
                    "unavailable"
                ):
                    return last
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise SmokeError(f"等待上游不可用状态超时：{last}")


async def _full_smoke(
    ctrl: ServiceController,
    key: str,
    args,
) -> dict:
    evidence: dict = {"mode": "full", "run_at": _now(), "checks": {}}
    env_with = ctrl.env_with_key(key)
    env_without = ctrl.env_without_key()

    adapter = AgentScopeHTTPClient(
        ctrl.asc_url,
        USER_ID,
        request_timeout=60.0,
        connect_timeout=10.0,
    )
    agent_id: str | None = None

    ctrl.start("agentscope", env_with)
    ctrl.start("nexus-backend", env_with)

    try:
        # 1) 服务健康与模型配置
        asc_health = await _wait_health(
            f"{ctrl.asc_url}/nexus/health",
            HEALTH_TIMEOUT,
        )
        if asc_health.get("model_configured") is not True:
            raise SmokeError("AgentScope 报告模型未配置")
        backend_health = await _wait_health(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        if backend_health.get("status") != "ok":
            raise SmokeError(f"后端健康非 ok：{backend_health}")
        evidence["agentscope"] = {
            "base_url": ctrl.asc_url,
            "version": backend_health["agentscope"]["version"],
        }
        evidence["backend"] = {
            "base_url": ctrl.backend_url,
            "status": backend_health["status"],
        }
        evidence["checks"]["model_configured"] = True
        evidence["checks"]["backend_health_ok"] = True

        defaults = await adapter.service_defaults()
        if defaults.get("model_configured") is not True:
            raise SmokeError("/nexus/defaults 报告模型未配置")
        chat_model_config = defaults["chat_model_config"]
        evidence["default_chat_model_config"] = chat_model_config

        # 2) 原生测试 Agent + 两个 Session
        agent_id = await adapter.create_agent(
            name=f"smoke-runtime-{uuid.uuid4().hex[:8]}",
            system_prompt=(
                "你是一个可靠的测试助手。用户会让你记住一个口令，之后询问"
                "时必须准确回答该口令。回答简洁，只回答被要求的内容。"
            ),
        )
        listing = await adapter.list_agents()
        matched = next(a for a in listing["agents"] if a["id"] == agent_id)
        evidence["agent"] = {
            "id": agent_id,
            "data_id": matched["data"]["id"],
            "two_ids_differ": matched["data"]["id"] != matched["id"],
        }
        evidence["checks"]["agent_top_level_id"] = (
            matched["data"]["id"] != matched["id"]
        )

        session_a = await adapter.create_session(
            agent_id=agent_id,
            name="smoke-A",
            chat_model_config=chat_model_config,
        )
        session_b = await adapter.create_session(
            agent_id=agent_id,
            name="smoke-B",
            chat_model_config=chat_model_config,
        )
        views = (await adapter.list_sessions(agent_id))["sessions"]
        by_id = {v["session"]["id"]: v for v in views}
        ws_a = by_id[session_a]["session"]["config"]["workspace_id"]
        ws_b = by_id[session_b]["session"]["config"]["workspace_id"]
        if ws_a == ws_b:
            raise SmokeError("两个 Session 的 PER_SESSION workspace 相同")
        evidence["sessions"] = [
            {"role": "A", "id": session_a, "workspace_id": ws_a},
            {"role": "B", "id": session_b, "workspace_id": ws_b},
        ]
        evidence["checks"]["per_session_workspace_isolated"] = True

        # 3) 随机口令两轮上下文
        token = "ZQ" + secrets.token_hex(4)
        evidence["token"] = token

        round_a1 = await run_round(
            adapter,
            agent_id,
            session_a,
            f"请记住这个口令：{token}。只需回复：已记住",
        )
        round_a2 = await run_round(
            adapter,
            agent_id,
            session_a,
            "请只回复刚才我让你记住的口令本身，不要有其他内容。",
        )
        round_b1 = await run_round(
            adapter,
            agent_id,
            session_b,
            "我让你记住的口令是什么？如果你不知道，就回答：不知道。",
        )
        evidence["rounds"] = {
            "A1": _round_evidence(round_a1),
            "A2": _round_evidence(round_a2),
            "B1": _round_evidence(round_b1),
        }

        recalled_a = token.lower() in round_a2["reply_text"].lower()
        leaked_b = token.lower() in round_b1["reply_text"].lower()
        if round_a2["finished_reason"] != "completed":
            raise SmokeError(
                f"A 第二轮未正常完成：{round_a2['finished_reason']}",
            )
        if not recalled_a:
            raise SmokeError("A 第二轮未能回忆口令，上下文未生效")
        if leaked_b:
            raise SmokeError("B Session 错误获得了 A 的上下文（口令泄露）")
        evidence["checks"]["round2_context_recall"] = recalled_a
        evidence["checks"]["session_b_isolated"] = not leaked_b

        # 4) AgentScope 真实进程 stop/start，无 env 密钥继续第三轮
        ctrl.stop("agentscope", env_with)
        ctrl.start("agentscope", env_without)
        restarted = await _wait_health(
            f"{ctrl.asc_url}/nexus/health",
            HEALTH_TIMEOUT,
        )
        if restarted.get("model_configured") is not True:
            raise SmokeError("重启（无 env 密钥）后持久凭据未被复用")
        evidence["checks"]["credential_reused_without_env"] = True

        # 进程已重启：丢弃旧连接池，避免复用半开连接。
        await adapter.aclose()
        adapter = AgentScopeHTTPClient(
            ctrl.asc_url,
            USER_ID,
            request_timeout=60.0,
            connect_timeout=10.0,
        )

        round_a3 = await run_round(
            adapter,
            agent_id,
            session_a,
            "请只回复刚才我让你记住的口令本身，不要有其他内容。",
        )
        evidence["rounds"]["A3_after_restart"] = _round_evidence(round_a3)
        recalled_after_restart = (
            token.lower() in round_a3["reply_text"].lower()
        )
        if not recalled_after_restart:
            raise SmokeError("重启后同一 Session 未能回忆口令")
        evidence["checks"]["restart_persistence_recall"] = (
            recalled_after_restart
        )

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        if agent_id is not None:
            try:
                await adapter.delete_agent(agent_id, ignore_missing=True)
                evidence.setdefault("checks", {})["cleanup_agent_deleted"] = True
            except UpstreamError as exc:
                evidence.setdefault("cleanup_error", str(exc))
        await adapter.aclose()
        # 先停后端（依赖上游），再停 AgentScope。
        try:
            ctrl.stop("nexus-backend", env_without)
        except SmokeError:
            pass
        try:
            ctrl.stop("agentscope", env_without)
        except SmokeError:
            pass
    return evidence


def _round_evidence(round_result: dict) -> dict:
    return {
        "reply_id": round_result["reply_id"],
        "finished_reason": round_result["finished_reason"],
        "event_types": round_result["event_types"],
        "reply_preview": round_result["reply_preview"],
    }


def _write_evidence(evidence: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-runtime-{stamp}.json"
    evidence["evidence_path"] = str(out.relative_to(ROOT))
    evidence["key_recorded"] = False
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus 真实运行时联调")
    parser.add_argument(
        "--connectivity-only",
        action="store_true",
        help="仅验证后端真实健康连通，不调用模型（无需密钥）。",
    )
    parser.add_argument(
        "--keep-evidence",
        action="store_true",
        help="保留本次临时数据目录用于排查。",
    )
    args = parser.parse_args(argv)

    # 只读取仓库根 .env（如存在）；不读取用户 Home 凭据。
    load_dotenv(ROOT / ".env", override=False)
    key = (os.environ.get("AGENTSCOPE_MODEL_API_KEY") or "").strip()
    data_root = Path(tempfile.mkdtemp(prefix="nexus-runtime-"))
    asc_port = free_port()
    backend_port = free_port()
    while backend_port == asc_port:
        backend_port = free_port()
    ctrl = ServiceController(
        data_root=data_root,
        asc_port=asc_port,
        backend_port=backend_port,
    )

    if not args.connectivity_only and not key:
        evidence = {
            "mode": "full",
            "run_at": _now(),
            "status": "unverified",
            "reason": (
                "未提供 AGENTSCOPE_MODEL_API_KEY，真实模型联调未运行。"
                "不 fallback 假模型、不伪造回复。"
            ),
        }
        out = _write_evidence(evidence)
        print(f"[未验证] {evidence['reason']}")
        print(f"证据已写入 {out}")
        shutil.rmtree(data_root, ignore_errors=True)
        return 2

    try:
        if args.connectivity_only:
            evidence = asyncio.run(_connectivity_only(ctrl, args))
        else:
            evidence = asyncio.run(_full_smoke(ctrl, key, args))
    except Exception as exc:  # noqa: BLE001
        evidence = {
            "mode": "connectivity_only" if args.connectivity_only else "full",
            "run_at": _now(),
            "status": "failed",
            "error": _safe_error(exc),
        }

    if args.keep_evidence:
        evidence["temp_data_dir"] = str(data_root)
    out = _write_evidence(evidence)
    status = evidence.get("status")
    print(f"[{status}] 证据已写入 {out}")
    if not args.keep_evidence:
        shutil.rmtree(data_root, ignore_errors=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
