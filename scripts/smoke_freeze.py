#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""运行 admission 冻结 / quiesce 隔离进程冒烟（OpenSpec 任务 3.2.3.1）。

用交付的 ``scripts/services.py`` 在隔离 data_dir/端口上实际启动两个真实进程
（官方 AgentScope 服务 + Nexus 后端），并用真实模型驱动一轮**在途 cancel-tail**
的真实运行，验证删除前置屏障：

1. **基线**：真实多轮运行必须正常终态（``finished_reason == completed``）且有
   有效真实模型输出（非空正文），不是 error/canceled；
2. **在途 cancel-tail**：另发一个可观察的长运行，真实观察到 ``REPLY_START`` /
   ``MODEL_CALL_START`` / 输出且会话仍 busy，才 ``prepare-delete`` 并 ``freeze``；
   冻结报告必须证明**确实捕获在途真实 Task**（``captured_chat_alive_at_capture``）
   并最终结束，绝不在会话恰好已 idle 时也算 cancel-tail；
3. freeze 完成后**直接读取最终历史**，核对本轮最终消息内容与终态、真实 State，
   并核对独立索引元数据（报告 ``pending`` 为空）；不以 ``REPLY_END``/idle 作为
   唯一持久提交证明，不记录全文/凭据；
4. AgentScope 服务真实 stop/start：服务代次（``generation_id``）必须改变，持久
   ``deleting``/操作身份与运行拒绝仍然生效，重试 quiesce 仍真实确认；
5. 冻结期间真实 ``POST /chat`` 被 admission 拒绝，已有历史不被追加。
6. 两服务 stop 失败则冒烟失败、保留数据与错误，不删除正在使用目录；缺模型凭据
   仅 **skipped**（退出码 2），绝不 fallback 假模型、绝不伪 passed。

需要 ``--with-model`` 且已由 Root 安全注入 ``AGENTSCOPE_MODEL_API_KEY``；未提供
时明确 skipped。绝不读取用户 Home 凭据、绝不触碰默认 ``data/``、绝不在证据中
记录密钥或消息全文。

    uv run python scripts/smoke_freeze.py --with-model
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
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-service"))
sys.path.insert(0, str(ROOT / "backend"))

from nexus_backend.adapter import AgentScopeHTTPClient, UpstreamError  # noqa: E402

EVIDENCE_DIR = ROOT / "evidence" / "phase3"
USER_ID = "nexus-service"
HEALTH_TIMEOUT = 60.0
RUN_TIMEOUT = 240.0
INFLIGHT_TIMEOUT = 120.0
IDLE_TIMEOUT = 180.0
PREVIEW_LIMIT = 160

# 长运行提示：要求模型逐行输出大量数字，确保观察到输出时仍在生成，便于在
# 真实在途状态发起 prepare-delete + freeze。
LONG_RUN_PROMPT = (
    "请逐行写出从 1 到 800 的每个整数，每行只写一个数字，不要省略，"
    "不要合并成一行，不要解释。"
)


class SmokeError(RuntimeError):
    """冒烟断言失败。"""


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, (SmokeError, UpstreamError)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _input_msg(text: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "name": "人类",
        "role": "user",
        "content": [{"type": "text", "text": text}],
    }


def _assistant_text(message: dict) -> str:
    """从持久化历史消息提取拼接后的正文；非文本块忽略。"""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _preview(text: str) -> str:
    return text[:PREVIEW_LIMIT]


def _require_completed_round(result: dict, *, label: str) -> None:
    """基线轮必须是正常终态且有有效真实模型输出，否则冒烟失败。"""
    if result.get("finished_reason") != "completed":
        raise SmokeError(
            f"{label}未正常完成：finished_reason="
            f"{result.get('finished_reason')!r}",
        )
    if not (result.get("text") or "").strip():
        raise SmokeError(f"{label}没有有效真实模型输出（空正文）")


def _require_persisted_reply(
    messages: list[dict],
    reply_id: str | None,
    *,
    label: str,
) -> dict:
    """必须在持久历史中找到本轮 assistant 消息，绝不退回流式正文冒充持久。"""
    if not isinstance(reply_id, str) or not reply_id:
        raise SmokeError(f"{label}缺少可信 reply_id，无法核对持久消息")
    reply = next((m for m in messages if m.get("id") == reply_id), None)
    if reply is None:
        raise SmokeError(
            f"{label}在持久历史中找不到本轮 assistant 消息，"
            "不得以流式正文冒充持久提交",
        )
    return reply


def _require_cancel_tail_round(
    result: dict,
    *,
    label: str,
    observed_prefix: str,
) -> None:
    """真实 cancel-tail 终态必须是 interrupted 且保留已观测真实输出。

    ``completed``/``error``/``interrupted`` 之外的终态、空正文、或最终正文
    丢掉了已观测前缀，都不得算该取消验收通过；``error`` 绝不被当成正常取消。
    """
    reason = result.get("finished_reason")
    if reason != "interrupted":
        raise SmokeError(
            f"{label}不是真实取消尾终态（finished_reason={reason!r}），"
            "completed/error/None 均不得算取消验收通过",
        )
    text = result.get("text") or ""
    if not text.strip():
        raise SmokeError(f"{label}最终消息正文为空，不得视为持久提交")
    if observed_prefix and observed_prefix not in text:
        raise SmokeError(
            f"{label}最终正文未保留已观测前缀/内容（不得只留中断占位）",
        )


def _require_cancel_tail_report(report: dict, *, session_id: str) -> dict:
    """冻结报告必须证明捕获的是**在途**真实 Task，且最终已排空并确认。"""
    if report.get("execution_drained") is not True:
        raise SmokeError(f"冻结未排空：{report.get('execution_drained')}")
    if report.get("persistence_verified") is not True:
        raise SmokeError(f"冻结落盘未确认：{report.get('persistence_verified')}")
    if report.get("accepted_input_archival") != "not_verified":
        raise SmokeError("accepted_input_archival 必须显式 not_verified")
    sessions = report.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        raise SmokeError("冻结报告缺少 session 存活事实")
    session = next(
        (s for s in sessions if s.get("session_id") == session_id),
        None,
    )
    if session is None:
        raise SmokeError("冻结报告缺少目标 session 存活事实")
    if session.get("captured_chat_alive_at_capture") is not True:
        raise SmokeError(
            "冻结未捕获在途真实 Task（可能已 idle），不算 cancel-tail",
        )
    if session.get("captured_chat_alive") is not False:
        raise SmokeError("冻结返回时捕获的真实 Task 仍未结束")
    if session.get("pending"):
        raise SmokeError("冻结报告仍保留未确认 pending（索引未清）")
    return session


def _finalize_status(evidence: dict, stop_failures: list[str]) -> bool:
    """停止失败则冒烟失败；保留错误供证据。返回是否应保留临时目录。"""
    if stop_failures:
        evidence["stop_failures"] = list(stop_failures)
        evidence["status"] = "failed"
        evidence.setdefault("error", stop_failures[0])
    return bool(stop_failures) or evidence.get("status") != "passed"


def _run_services(
    action: str,
    service: str,
    env: dict,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "scripts/services.py", action, service],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _start_service(service: str, env: dict) -> None:
    proc = _run_services("start", service, env)
    if proc.returncode != 0:
        raise SmokeError(
            f"启动 {service} 失败（exit {proc.returncode}）：{proc.stderr.strip()}",
        )


def _stop_service(service: str, env: dict) -> None:
    """停止服务；无法确认退出必须显式失败（不视为成功）。"""
    proc = _run_services("stop", service, env)
    if proc.returncode != 0:
        raise SmokeError(
            f"停止 {service} 失败（exit {proc.returncode}）：{proc.stderr.strip()}",
        )


def _stop_all(env: dict) -> list[str]:
    """先停后端（依赖上游）再停 AgentScope；返回失败描述（可能为空）。"""
    failures: list[str] = []
    for service in ("nexus-backend", "agentscope"):
        try:
            _stop_service(service, env)
        except SmokeError as exc:
            failures.append(str(exc))
    return failures


async def _wait_health(url: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last = "无响应"
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


async def _wait_idle(adapter, agent_id: str, session_id: str) -> str | None:
    deadline = time.monotonic() + IDLE_TIMEOUT
    last: str | None = None
    while time.monotonic() < deadline:
        status = await adapter.get_session_status(agent_id, session_id)
        last = status.get("status")
        if last == "idle":
            return last
        await asyncio.sleep(0.5)
    raise SmokeError(f"等待会话 idle 超时，最后状态：{last}")


async def _run_round(
    adapter,
    agent_id: str,
    session_id: str,
    text: str,
    *,
    timeout: float = RUN_TIMEOUT,
) -> dict:
    """触发热运行并按 event_cursor 订阅 SSE 直到 REPLY_END，返回终态事实。"""
    history = await adapter.list_messages(agent_id, session_id)
    cursor = history.get("event_cursor")
    trigger = await adapter.trigger_chat(agent_id, session_id, _input_msg(text))
    if trigger.get("status") != "started":
        raise SmokeError(f"POST /chat 未返回 started：{trigger}")

    event_types: list[str] = []
    text_deltas: list[str] = []
    end_event: dict | None = None
    stream = adapter.stream_events(agent_id, session_id, after=cursor)
    try:
        async def consume() -> None:
            nonlocal end_event
            async for event in stream:
                kind = event.get("type")
                event_types.append(kind)
                if kind == "TEXT_BLOCK_DELTA" and isinstance(
                    event.get("delta"),
                    str,
                ):
                    text_deltas.append(event["delta"])
                if kind == "REPLY_END":
                    end_event = event
                    return

        await asyncio.wait_for(consume(), timeout=timeout)
    finally:
        await stream.aclose()
    if end_event is None:
        raise SmokeError("SSE 未收到 REPLY_END")
    return {
        "finished_reason": end_event.get("finished_reason"),
        "reply_id": end_event.get("reply_id"),
        "event_types": event_types,
        "text": "".join(text_deltas),
    }


async def _start_inflight_round(
    adapter,
    agent_id: str,
    session_id: str,
    text: str,
    *,
    timeout: float = INFLIGHT_TIMEOUT,
) -> dict:
    """触发可观察的长运行，直到真实观察到在途推理与输出且会话仍 busy。

    返回 :class:`dict`：``reply_id`` / 事件类型 / 已观察正文 / busy 状态。
    只消费到确认在途即关闭 SSE（运行仍服务端继续，不因断开被取消）。
    """
    history = await adapter.list_messages(agent_id, session_id)
    cursor = history.get("event_cursor")
    trigger = await adapter.trigger_chat(agent_id, session_id, _input_msg(text))
    if trigger.get("status") != "started":
        raise SmokeError(f"POST /chat 未返回 started：{trigger}")

    state: dict = {
        "reply_id": None,
        "event_types": [],
        "text": "",
        "model_call_started": False,
    }
    inflight = asyncio.Event()
    stream = adapter.stream_events(agent_id, session_id, after=cursor)
    try:
        async def consume() -> None:
            async for event in stream:
                kind = event.get("type")
                state["event_types"].append(kind)
                if kind == "REPLY_START":
                    reply_id = event.get("reply_id")
                    if isinstance(reply_id, str):
                        state["reply_id"] = reply_id
                if kind == "MODEL_CALL_START":
                    state["model_call_started"] = True
                if kind == "TEXT_BLOCK_DELTA" and isinstance(
                    event.get("delta"),
                    str,
                ):
                    state["text"] += event["delta"]
                if state["model_call_started"] and state["text"].strip():
                    inflight.set()
                    return
                if kind == "REPLY_END":
                    return

        await asyncio.wait_for(consume(), timeout=timeout)
    finally:
        await stream.aclose()

    if not inflight.is_set():
        raise SmokeError(
            "长运行未观察到在途 MODEL_CALL_START/输出，无法证明 cancel-tail",
        )
    status = await adapter.get_session_status(agent_id, session_id)
    if status.get("status") == "idle":
        raise SmokeError("长运行在 freeze 前已 idle，不得当作 cancel-tail")
    if not isinstance(state["reply_id"], str) or not state["reply_id"]:
        raise SmokeError("长运行未观察到可信 REPLY_START reply_id")
    state["status"] = status.get("status")
    return state


async def _smoke(ctrl: dict, key: str) -> dict:
    evidence: dict = {"mode": "with_model", "run_at": _now(), "checks": {}}
    internal_token = ctrl["internal_token"]
    asc_url = ctrl["asc_url"]
    backend_url = ctrl["backend_url"]
    env = dict(ctrl["env"])
    env["AGENTSCOPE_MODEL_API_KEY"] = key

    adapter = AgentScopeHTTPClient(asc_url, USER_ID, request_timeout=60.0)
    stop_failures: list[str] = []
    agent_id = None
    try:
        for service in ("agentscope", "nexus-backend"):
            _start_service(service, env)
        asc_health = await _wait_health(f"{asc_url}/nexus/health", HEALTH_TIMEOUT)
        await _wait_health(f"{backend_url}/api/health", HEALTH_TIMEOUT)
        generation_before = asc_health.get("generation_id")
        if not generation_before:
            raise SmokeError("AgentScope 未返回服务代次 generation_id")
        evidence["checks"]["services_ready"] = True
        evidence["generation_before"] = generation_before

        owner = str(uuid.uuid4())
        backend_client = httpx.AsyncClient(
            base_url=backend_url,
            headers={"X-Nexus-Owner": owner},
            timeout=60.0,
            trust_env=False,
        )
        internal_headers = {
            "X-Nexus-Internal-Token": internal_token,
            "X-User-ID": USER_ID,
        }
        try:
            employee_resp = await backend_client.post(
                "/api/employees",
                json={
                    "name": f"冻结冒烟{uuid.uuid4().hex[:6]}",
                    "domain": "冻结冒烟领域",
                    "responsibilities": (
                        "你是一个测试助手。回答简洁，只回答被要求的内容。"
                    ),
                    "instructions": "只回答被要求的内容，不要扩展。",
                    "avatar": "🤖",
                },
            )
            if employee_resp.status_code != 201:
                raise SmokeError(f"创建员工失败：{employee_resp.text}")
            employee = employee_resp.json()
            agent_id = employee["agent_id"]
            conv_resp = await backend_client.post(
                "/api/conversations",
                json={
                    "employee_id": employee["id"],
                    "title": "freeze-smoke",
                    "client_key": uuid.uuid4().hex,
                },
            )
            if conv_resp.status_code != 201:
                raise SmokeError(f"创建会话失败：{conv_resp.text}")
            conversation = conv_resp.json()
            session_id = conversation["session_id"]
            evidence["agent_id"] = agent_id
            evidence["session_id"] = session_id

            # 1) 基线：真实正常终态 + 有效真实模型输出。
            baseline = await _run_round(
                adapter,
                agent_id,
                session_id,
                "请只回复：ok",
            )
            await _wait_idle(adapter, agent_id, session_id)
            baseline_messages = await adapter.iter_all_messages(
                agent_id,
                session_id,
            )
            baseline_reply = _require_persisted_reply(
                baseline_messages,
                baseline["reply_id"],
                label="基线运行",
            )
            baseline_history = {
                "finished_reason": baseline_reply.get("finished_reason"),
                "text": _assistant_text(baseline_reply),
            }
            _require_completed_round(baseline_history, label="基线运行")
            evidence["checks"]["baseline_completed"] = True
            evidence["baseline"] = {
                "finished_reason": baseline_history["finished_reason"],
                "reply_preview": _preview(baseline_history["text"]),
            }

            # 2) 在途长运行：真实观察到 MODEL_CALL_START/输出且仍 busy。
            inflight = await _start_inflight_round(
                adapter,
                agent_id,
                session_id,
                LONG_RUN_PROMPT,
            )
            reply_id = inflight["reply_id"]
            evidence["checks"]["inflight_observed"] = True
            evidence["inflight"] = {
                "reply_id": reply_id,
                "model_call_started": inflight["model_call_started"],
                "status_before_freeze": inflight["status"],
                "observed_preview": _preview(inflight["text"]),
                "observed_length": len(inflight["text"]),
            }

            # 3) 落库删除事实（deleting + 稳定操作身份）。
            prep = await backend_client.post(
                "/api/internal/prepare-delete",
                json={"employee_id": employee["id"]},
                headers=internal_headers,
            )
            if prep.status_code != 200:
                raise SmokeError(f"prepare-delete 失败：{prep.text}")
            plan = prep.json()
            operation_id = plan["operation_id"]
            if plan["sync_state"] != "deleting" or not operation_id:
                raise SmokeError(f"删除事实未落库：{plan}")
            evidence["operation_id"] = operation_id
            evidence["checks"]["deleting_persisted"] = True

            fact = await backend_client.post(
                "/api/internal/run-fact",
                json={"agent_id": agent_id, "session_id": session_id},
                headers=internal_headers,
            )
            if fact.status_code != 200 or fact.json()["deleting"] is not True:
                raise SmokeError(f"run-fact 未反映 deleting：{fact.text}")
            if fact.json()["operation_id"] != operation_id:
                raise SmokeError("run-fact 操作身份不一致")
            evidence["checks"]["run_fact_deleting"] = True

            # 冻结期间新运行必须被 admission 拒绝。
            try:
                blocked = await adapter.trigger_chat(
                    agent_id,
                    session_id,
                    _input_msg("不应运行"),
                )
            except UpstreamError as exc:
                if exc.status_code != 409:
                    raise
                evidence["checks"]["admission_rejected_deleting"] = True
            else:
                raise SmokeError(f"admission 未拒绝删除中的运行：{blocked}")
        finally:
            await backend_client.aclose()

        # 4) 冻结并排空在途真实运行。
        freeze = await adapter.freeze_agent(
            operation_id=operation_id,
            agent_id=agent_id,
            session_ids=[session_id],
            token=internal_token,
            timeout_secs=30.0,
        )
        session_facts = _require_cancel_tail_report(freeze, session_id=session_id)
        evidence["freeze_report"] = {
            "execution_drained": freeze.get("execution_drained"),
            "persistence_verified": freeze.get("persistence_verified"),
            "accepted_input_archival": freeze.get("accepted_input_archival"),
            "captured_chat_alive_at_capture": session_facts.get(
                "captured_chat_alive_at_capture",
            ),
            "captured_chat_alive": session_facts.get("captured_chat_alive"),
            "pending": session_facts.get("pending"),
        }
        evidence["checks"]["cancel_tail_captured"] = True

        # 5) freeze 后直接读取最终历史：核对本轮最终消息内容/终态与真实落盘。
        #    真实 cancel-tail 必须终态 interrupted，且最终正文保留已观测前缀，
        #    error/completed/空正文一律不得算该取消验收通过。
        final_messages = await adapter.iter_all_messages(agent_id, session_id)
        final_reply = _require_persisted_reply(
            final_messages,
            reply_id,
            label="冻结后本轮",
        )
        final_result = {
            "finished_reason": final_reply.get("finished_reason"),
            "text": _assistant_text(final_reply),
        }
        _require_cancel_tail_round(
            final_result,
            label="冻结后本轮",
            observed_prefix=inflight["text"],
        )
        evidence["final_round"] = {
            "reply_id": reply_id,
            "finished_reason": final_result["finished_reason"],
            "reply_preview": _preview(final_result["text"]),
            "reply_length": len(final_result["text"]),
            "observed_prefix_retained": bool(inflight["text"])
            and inflight["text"] in final_result["text"],
        }
        evidence["checks"]["final_history_persisted"] = True
        evidence["checks"]["cancel_tail_interrupted"] = True

        # 6) 真实 stop/start：服务代次改变，持久 deleting/op 仍拒绝运行，
        #    重试 quiesce 仍真实确认。
        _stop_service("agentscope", env)
        _start_service("agentscope", env)
        restarted = await _wait_health(f"{asc_url}/nexus/health", HEALTH_TIMEOUT)
        generation_after = restarted.get("generation_id")
        if not generation_after or generation_after == generation_before:
            raise SmokeError("AgentScope 重启后服务代次未改变")
        evidence["generation_after"] = generation_after
        evidence["checks"]["generation_changed"] = True

        # 进程已重启：丢弃旧连接池，避免复用半开连接。
        await adapter.aclose()
        adapter = AgentScopeHTTPClient(
            asc_url,
            USER_ID,
            request_timeout=60.0,
        )

        async with httpx.AsyncClient(
            base_url=backend_url,
            timeout=30.0,
            trust_env=False,
        ) as backend_client:
            fact_after = await backend_client.post(
                "/api/internal/run-fact",
                json={"agent_id": agent_id, "session_id": session_id},
                headers=internal_headers,
            )
            body = fact_after.json()
            if (
                fact_after.status_code != 200
                or body.get("deleting") is not True
                or body.get("operation_id") != operation_id
            ):
                raise SmokeError(f"重启后 run-fact 删除事实丢失：{fact_after.text}")
        evidence["checks"]["deleting_persisted_after_restart"] = True

        # 新服务代次仍拒绝删除中的运行。
        try:
            blocked_after = await adapter.trigger_chat(
                agent_id,
                session_id,
                _input_msg("重启后不应运行"),
            )
        except UpstreamError as exc:
            if exc.status_code != 409:
                raise
            evidence["checks"]["admission_rejected_after_restart"] = True
        else:
            raise SmokeError(f"重启后 admission 未拒绝删除中的运行：{blocked_after}")

        # 重试 quiesce：无在途运行，但必须仍真实确认落盘。
        retry = await adapter.freeze_agent(
            operation_id=operation_id,
            agent_id=agent_id,
            session_ids=[session_id],
            token=internal_token,
            timeout_secs=15.0,
        )
        if (
            retry.get("execution_drained") is not True
            or retry.get("persistence_verified") is not True
            or retry.get("accepted_input_archival") != "not_verified"
        ):
            raise SmokeError(f"重启后重试 quiesce 未真实确认：{retry}")
        evidence["retry_quiesce"] = {
            "execution_drained": retry.get("execution_drained"),
            "persistence_verified": retry.get("persistence_verified"),
            "accepted_input_archival": retry.get("accepted_input_archival"),
        }
        evidence["checks"]["retry_quiesce_verified"] = True

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        await adapter.aclose()
        stop_failures = _stop_all(env)

    _finalize_status(evidence, stop_failures)
    return evidence


def _write_evidence(evidence: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-freeze-{stamp}.json"
    evidence["evidence_path"] = str(out.relative_to(ROOT))
    evidence["key_recorded"] = False
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行冻结/quiesce 冒烟")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="注入 Root 提供的 AGENTSCOPE_MODEL_API_KEY 运行真实模型冒烟。",
    )
    parser.add_argument("--keep-evidence", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env", override=False)
    key = (os.environ.get("AGENTSCOPE_MODEL_API_KEY") or "").strip()
    if not args.with_model or not key:
        evidence = {
            "mode": "with_model",
            "run_at": _now(),
            "status": "skipped",
            "reason": (
                "未提供 --with-model 或 AGENTSCOPE_MODEL_API_KEY，真实模型冻结"
                "冒烟未运行；不 fallback 假模型。"
            ),
        }
        out = _write_evidence(evidence)
        print(f"[skipped] {evidence['reason']}")
        print(f"证据已写入 {out}")
        return 2

    data_root = Path(tempfile.mkdtemp(prefix="nexus-freeze-"))
    asc_port = free_port()
    backend_port = free_port()
    while backend_port == asc_port:
        backend_port = free_port()
    internal_token = secrets.token_urlsafe(24)
    env = dict(os.environ)
    env.update(
        {
            "AGENTSCOPE_SERVICE_HOST": "127.0.0.1",
            "AGENTSCOPE_SERVICE_PORT": str(asc_port),
            "AGENTSCOPE_DATA_DIR": str(data_root / "agentscope"),
            "AGENTSCOPE_INTERNAL_USER_ID": USER_ID,
            "AGENTSCOPE_SERVICE_URL": f"http://127.0.0.1:{asc_port}",
            "NEXUS_BACKEND_HOST": "127.0.0.1",
            "NEXUS_BACKEND_PORT": str(backend_port),
            "NEXUS_BACKEND_DATA_DIR": str(data_root / "backend"),
            "NEXUS_GUARD_URL": f"http://127.0.0.1:{backend_port}",
            "NEXUS_INTERNAL_TOKEN": internal_token,
        },
    )
    ctrl = {
        "env": env,
        "asc_url": f"http://127.0.0.1:{asc_port}",
        "backend_url": f"http://127.0.0.1:{backend_port}",
        "internal_token": internal_token,
    }
    try:
        evidence = asyncio.run(_smoke(ctrl, key))
    except Exception as exc:  # noqa: BLE001
        evidence = {
            "mode": "with_model",
            "run_at": _now(),
            "status": "failed",
            "error": _safe_error(exc),
        }

    status = evidence.get("status")
    # 失败/停止失败一律保留临时数据目录（含日志与证据），绝不删除正在使用
    # 或承载失败证据的目录。
    preserve = args.keep_evidence or status != "passed"
    evidence["temp_data_dir"] = str(data_root)
    out = _write_evidence(evidence)
    print(f"[{status}] 证据已写入 {out}")
    if preserve:
        print(f"已保留临时数据目录：{data_root}")
    else:
        shutil.rmtree(data_root, ignore_errors=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
