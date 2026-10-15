#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nexus 资源目录 / 员工资源默认关联 / 共同 Session 绑定 / 运行 guard 冒烟。

用交付的 ``scripts/services.py`` **实际启动/停止/重启**两个真实进程，验证：

1. 真实资源目录投影（空 Skills/MCP 为 available 空态；未配置知识库 manager
   时为 unavailable，不伪装空目录）。
2. 独立启动一个测试专用真实无状态 Streamable HTTP MCP（``is_stateful=False``、
   ``http_mcp``、``enable_tools`` 精确选择一个工具），通过官方
   ``POST /workspace/mcp`` 建立 library，员工默认关联该工具，新建 Conversation
   时通过 from-library 真实装载并在 workspace 读回健康且仅含所选工具。
3. Conversation 与员工 Agent 顶层 id 绑定、owner 归属、能力 ID 快照。
4. 运行 guard：内部授权端点正确放行；员工禁用后拒绝，且直接调用原生 ``/chat``
   也因 guard 无法完成运行（不产生 assistant 回复）。
5. 两个真实进程重启后员工与 Conversation 绑定、原生 Session 持久恢复，并
   实际重新发现 workspace 所选 MCP；``--with-model`` 时重启后真实再次调用
   所选工具。

不读取用户 Home 或真实凭据；密钥不记录。证据写入 ``evidence/phase3/``。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "backend"))

from nexus_backend.adapter import AgentScopeHTTPClient, UpstreamError  # noqa: E402
from mcp_fixture_http import (  # noqa: E402
    HTTP_MCP_NAME as MCP_NAME,
    SELECTED_TOOL,
    http_mcp_declaration,
    running_http_mcp,
)
from smoke_runtime import (  # noqa: E402
    HEALTH_TIMEOUT,
    ServiceController,
    SmokeError,
    _safe_error,
    _wait_health,
    free_port,
)

USER_ID = "nexus-service"
EXPECTED_VERSION = "2.0.10"
EVIDENCE_DIR = ROOT / "evidence" / "phase3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _install_mcp(adapter, url: str) -> str:
    """用官方 POST /workspace/mcp 安装真实无状态 HTTP MCP，返回 library id。"""
    agent_id = await adapter.create_agent(
        name="resource-installer",
        system_prompt="installer",
    )
    session_id = await adapter.create_session(
        agent_id=agent_id,
        name="resource-installer",
    )
    declaration = http_mcp_declaration(url)
    declaration["enable_tools"] = [SELECTED_TOOL]
    await adapter.install_mcp(agent_id, session_id, declaration)
    mcps = await adapter.list_mcps()
    return next(m["id"] for m in mcps if m["name"] == MCP_NAME)


async def _collect_events(
    adapter,
    agent_id: str,
    session_id: str,
    *,
    text: str = "请按指示处理本条消息。",
    timeout: float = 45.0,
) -> dict:
    """先取 history cursor，触发后按 cursor 订阅，收集事件直到 REPLY_END。"""
    history = await adapter.list_messages(agent_id, session_id)
    cursor = history.get("event_cursor")
    trigger = await adapter.trigger_chat(
        agent_id,
        session_id,
        {
            "id": uuid.uuid4().hex,
            "name": "人类",
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    )
    event_types: list[str] = []
    end_event: dict | None = None
    text_deltas: list[str] = []
    tool_events: list[dict] = []
    try:
        stream = adapter.stream_events(agent_id, session_id, after=cursor)
        try:
            async def _consume() -> None:
                nonlocal end_event
                async for event in stream:
                    kind = event.get("type")
                    event_types.append(kind)
                    if kind == "TEXT_BLOCK_DELTA" and isinstance(
                        event.get("delta"),
                        str,
                    ):
                        text_deltas.append(event["delta"])
                    if isinstance(kind, str) and kind.startswith("TOOL_"):
                        tool_events.append(event)
                    if kind == "REPLY_END":
                        end_event = event
                        return

            await asyncio.wait_for(_consume(), timeout=timeout)
        finally:
            await stream.aclose()
    except Exception:  # noqa: BLE001 - 断流/超时不改变判定，仅记录
        pass
    # 必须真实回到 idle 才算本轮运行结束：断流/超时不算完成，绝不静默忽略。
    idle_status = await _wait_idle(adapter, agent_id, session_id)
    if idle_status != "idle":
        raise SmokeError(
            f"会话未在超时内回到 idle（实际 {idle_status}），无法确认本轮运行结束",
        )
    return {
        "trigger_status": trigger.get("status"),
        "event_types": event_types,
        "end_event": end_event,
        "finished_reason": (end_event or {}).get("finished_reason"),
        "text": "".join(text_deltas),
        "model_call_started": "MODEL_CALL_START" in event_types,
        "tool_call_started": "TOOL_CALL_START" in event_types,
        "tool_events": tool_events,
        "idle": idle_status,
    }


async def _wait_idle(
    adapter,
    agent_id: str,
    session_id: str,
    *,
    timeout: float = 30.0,
) -> str | None:
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        status = await adapter.get_session_status(agent_id, session_id)
        last = status.get("status")
        if last == "idle":
            return last
        await asyncio.sleep(0.5)
    return last


async def _run_enabled_round(adapter, agent_id: str, session_id: str) -> dict:
    """真实模型启用轮：确认发生模型调用并得到真实回复。"""
    result = await _run_round(
        adapter,
        agent_id,
        session_id,
        text="请只回复：联通正常。",
    )
    if not result["model_call_started"]:
        raise SmokeError("启用轮未发生 MODEL_CALL_START")
    if result["finished_reason"] != "completed":
        raise SmokeError(f"启用轮未正常完成：{result['finished_reason']}")
    if not result["text"].strip():
        raise SmokeError("启用轮未得到文本回复")
    return result


async def _run_mcp_tool_round(adapter, agent_id: str, session_id: str) -> dict:
    """真实模型工具轮：确认发生 TOOL_CALL 且工具结果来自真实 MCP。"""
    result = await _run_round(
        adapter,
        agent_id,
        session_id,
        text=(
            "请调用 compute_plus 工具把 20 和 22 相加，并且只根据工具返回的数字"
            "回答。不要自行计算。"
        ),
    )
    if not result["model_call_started"]:
        raise SmokeError("工具轮未发生 MODEL_CALL_START")
    if not result["tool_call_started"]:
        raise SmokeError("工具轮未发生 TOOL_CALL_START（模型未调用所选 MCP 工具）")
    if result["finished_reason"] != "completed":
        raise SmokeError(f"工具轮未正常完成：{result['finished_reason']}")
    if "42" not in result["text"]:
        raise SmokeError("工具轮回复未包含真实 MCP 计算结果 42")
    return result


async def _run_round(
    adapter,
    agent_id: str,
    session_id: str,
    *,
    text: str = "请按指示处理本条消息。",
) -> dict:
    return await _collect_events(adapter, agent_id, session_id, text=text)


def _assistant_text(message: dict) -> str:
    return "".join(
        block.get("text", "")
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


async def _confirm_admission_rejected(
    adapter,
    agent_id: str,
    session_id: str,
    baseline_ids: set,
) -> dict:
    """admission 明确拒绝（409）：读回历史/状态确认无新用户/assistant 执行。

    只有 ``409`` 才算「运行 admission 真实阻断」；读回必须证明没有新的用户或
    assistant 消息、会话未在运行。旧历史仅按 ID 差分归因，绝不污染。
    """
    messages = await adapter.iter_all_messages(agent_id, session_id)
    new_users = [
        message
        for message in messages
        if message.get("id") not in baseline_ids
        and message.get("role") == "user"
    ]
    new_assistants = [
        message
        for message in messages
        if message.get("id") not in baseline_ids
        and message.get("role") == "assistant"
    ]
    if new_users:
        raise SmokeError("admission 409 拒绝后仍出现新的用户消息，拒绝未真实阻断")
    if new_assistants:
        raise SmokeError(
            "admission 409 拒绝后仍出现新的 assistant 执行，拒绝未真实阻断",
        )
    status = await adapter.get_session_status(agent_id, session_id)
    if status.get("status") == "running":
        raise SmokeError("admission 409 拒绝后会话仍在运行")
    return {
        "trigger_status": "rejected",
        "admission_status": 409,
        "model_call_started": False,
        "event_types": [],
        "end_event": None,
        "finished_reason": None,
        "text": "",
        "new_user_messages": 0,
        "new_assistant_messages": 0,
        "new_assistant_texts": [],
        "session_status": status.get("status"),
    }


async def _run_guard_denied(adapter, agent_id: str, session_id: str) -> dict:
    """guard 应拒绝时触发原生 /chat：确认未发生模型调用、本轮无成功回复。

    **只归因本轮**：先按原生**完整 history** 取 baseline message IDs，再发唯一
    inputID/真实 SSE，待 native idle 与历史持久化后，仅检查本次禁用触发新产生
    的消息（按新 ID）。既有正常历史的回复绝不能被算作本轮 guard 回复，也不能
    因为 joined 里含一个 guard 词就吞掉本轮另一条正常新回复；本轮出现的原生
    error 记录（空/明确拒绝）不算成功回复，但任何新的正常成功回复都必须失败。

    两条真实拒绝路径都接受，且都必须读到真实阻断事实：

    * **admission 直接 409**（禁用/删除中，运行 admission 在原生 handler 前
      拒绝）：不得误当异常吞成成功，只有 409 走读回确认路径；其它上游错误如实
      抛出。
    * **原生 guard error**：``POST /chat`` 仍返回 ``started``，本轮 SSE 以
      ``finished_reason=error`` 结束且无 ``MODEL_CALL_START``——保留原有检查。
    """
    baseline_ids = {
        message.get("id")
        for message in await adapter.iter_all_messages(agent_id, session_id)
    }
    try:
        result = await _collect_events(
            adapter,
            agent_id,
            session_id,
            timeout=30.0,
        )
    except UpstreamError as exc:
        if exc.status_code != 409:
            raise
        return await _confirm_admission_rejected(
            adapter,
            agent_id,
            session_id,
            baseline_ids,
        )
    if result["model_call_started"]:
        raise SmokeError("guard 拒绝时仍发生了 MODEL_CALL_START")
    # 必须确有本轮 REPLY_END 且 finished_reason 明确为 error：None（断流/超时/
    # 未结束）一律不能当作“已确认 guard 拒绝”。
    if result["end_event"] is None:
        raise SmokeError("guard 拒绝轮未收到本轮 REPLY_END（断流/超时）")
    if result["finished_reason"] != "error":
        raise SmokeError(
            f"guard 拒绝轮 REPLY_END 非 error：{result['finished_reason']}",
        )

    messages = await adapter.iter_all_messages(agent_id, session_id)
    new_assistants = [
        message
        for message in messages
        if message.get("id") not in baseline_ids
        and message.get("role") == "assistant"
    ]
    new_texts = [_assistant_text(message) for message in new_assistants]
    result["new_assistant_messages"] = len(new_assistants)
    result["new_assistant_texts"] = new_texts
    # 逐条检查：空文本或明确 guard 拒绝的原生 error 记录可接受；任何新的正常
    # 成功回复都必须失败。绝不把先前成功历史与本轮混算。
    for text in new_texts:
        if text.strip() and "guard" not in text and "拒绝" not in text:
            raise SmokeError(
                "guard 拒绝时本轮产生了非 guard 的新成功回复："
                f"{new_texts}",
            )
    return result


async def _authorize(backend_url: str, token: str, agent_id: str, session_id: str):
    async with httpx.AsyncClient(
        base_url=backend_url,
        timeout=10.0,
        trust_env=False,
    ) as client:
        response = await client.post(
            "/api/internal/authorize-run",
            headers={"X-Nexus-Internal-Token": token},
            json={"agent_id": agent_id, "session_id": session_id},
        )
    return response


async def _run(
    ctrl: ServiceController,
    *,
    mcp_url: str,
    with_model: bool = False,
    key: str = "",
) -> dict:
    evidence: dict = {"mode": "full", "run_at": _now(), "checks": {}}
    env = ctrl.base_env()
    # 创建 Conversation 需要默认模型已配置。默认不做模型推理（guard 在模型
    # 调用前拦截），只用一个占位凭据让 /nexus/defaults 报告已配置；--with-model
    # 时使用 Root 注入的真实密钥并执行真实推理。
    if with_model and key:
        env["AGENTSCOPE_MODEL_API_KEY"] = key
    elif not (env.get("AGENTSCOPE_MODEL_API_KEY") or "").strip():
        env["AGENTSCOPE_MODEL_API_KEY"] = "nexus-smoke-placeholder-key"

    evidence["model_check"] = {
        "status": "skipped",
        "reason": (
            "未提供 AGENTSCOPE_MODEL_API_KEY，真实模型启用/工具轮未运行；"
            "本证据仅证明资源目录、Session 绑定与非模型 guard 边界。"
        ),
    }

    adapter = AgentScopeHTTPClient(
        ctrl.asc_url,
        USER_ID,
        request_timeout=60.0,
        connect_timeout=10.0,
    )
    owner = str(uuid.uuid4())
    try:
        ctrl.start("agentscope", env)
        ctrl.start("nexus-backend", env)
        backend_health = await _wait_health(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        if backend_health["agentscope"]["version"] != EXPECTED_VERSION:
            raise SmokeError("版本不符")
        asc_health = await _wait_health(
            f"{ctrl.asc_url}/nexus/health",
            HEALTH_TIMEOUT,
        )
        generation_before = asc_health.get("generation_id")
        if asc_health.get("guard_configured") is not True:
            raise SmokeError("AgentScope 未配置运行 guard")
        evidence["checks"]["backend_health_ok"] = True

        async with httpx.AsyncClient(
            base_url=ctrl.backend_url,
            headers={"X-Nexus-Owner": owner},
            timeout=60.0,
            trust_env=False,
        ) as client:
            # 1) 资源目录真实投影
            catalog = (await client.get("/api/resources")).json()
            evidence["catalog"] = catalog
            if catalog["skills"]["available"] is not True:
                raise SmokeError("Skills 目录应为 available 空态")
            if catalog["knowledge"]["available"] is not False:
                raise SmokeError("未配置 embedding 时知识库应为 unavailable")
            evidence["checks"]["catalog_real_state"] = True

            # 2) 真实无状态 HTTP MCP 安装 + 员工默认关联保存
            mcp_id = await _install_mcp(adapter, mcp_url)
            employee_resp = await client.post(
                "/api/employees",
                json={
                    "name": f"资源员工{uuid.uuid4().hex[:6]}",
                    "domain": "资源验证领域",
                    "responsibilities": (
                        "你需要按用户指示调用已有工具或直接回答。"
                    ),
                    "instructions": "只回答被要求的内容。",
                    "avatar": "🤖",
                    "tools": [mcp_id],
                },
            )
            if employee_resp.status_code != 201:
                raise SmokeError(
                    f"创建员工失败：{employee_resp.status_code} "
                    f"{employee_resp.text}",
                )
            employee = employee_resp.json()
            if employee["tools"] != [mcp_id]:
                raise SmokeError("员工默认工具关联未真实保存")
            evidence["employee"] = {
                "id": employee["id"],
                "tools": employee["tools"],
            }
            evidence["checks"]["employee_resource_default_saved"] = True

            # 3) 共同 Session 绑定与会话能力真实装载
            conv_resp = await client.post(
                "/api/conversations",
                json={
                    "employee_id": employee["id"],
                    "client_key": uuid.uuid4().hex,
                },
            )
            if conv_resp.status_code != 201:
                raise SmokeError(
                    f"新建会话失败：{conv_resp.status_code} {conv_resp.text}",
                )
            conversation = conv_resp.json()
            if conversation["status"] != "ready":
                raise SmokeError(f"会话未 ready：{conversation}")
            if conversation["agent_id"] != employee["agent_id"]:
                raise SmokeError("会话绑定的 Agent 与员工不一致")
            if conversation["capability_snapshot"]["tools"] != [mcp_id]:
                raise SmokeError("会话能力快照未记录所选工具")
            mcps = await adapter.list_workspace_mcps(
                employee["agent_id"],
                conversation["session_id"],
            )
            installed = next(
                (m for m in mcps if m["name"] == MCP_NAME),
                None,
            )
            if installed is None or installed.get("is_healthy") is not True:
                raise SmokeError("会话 workspace 未确认真实装载所选 MCP")
            evidence["conversation"] = {
                "id": conversation["id"],
                "session_id": conversation["session_id"],
                "agent_id": conversation["agent_id"],
                "capability_snapshot": conversation["capability_snapshot"],
                "mcp_healthy": True,
            }
            evidence["checks"]["session_binding_and_capability"] = True

            # 3a) 归因探针：启用时应真实进入模型调用（占位密钥会在调用后失败，
            # 但 MODEL_CALL_START 必须出现），用于与禁用后的“无模型调用”对照，
            # 避免把任意失败当成 guard 生效。
            enabled_probe = await _run_round(
                adapter,
                employee["agent_id"],
                conversation["session_id"],
                text="请只回复：探针。",
            )
            evidence["enabled_probe"] = {
                "model_call_started": enabled_probe["model_call_started"],
                "finished_reason": enabled_probe["finished_reason"],
            }
            if not enabled_probe["model_call_started"]:
                raise SmokeError(
                    "启用状态下原生 /chat 未发生 MODEL_CALL_START，"
                    "无法把禁用后的无调用归因于 guard",
                )

            # 3b) 可选真实模型：启用轮 + 所选 MCP 真实工具轮
            if with_model and key:
                enabled_round = await _run_enabled_round(
                    adapter,
                    employee["agent_id"],
                    conversation["session_id"],
                )
                tool_round = await _run_mcp_tool_round(
                    adapter,
                    employee["agent_id"],
                    conversation["session_id"],
                )
                evidence["model_check"] = {
                    "status": "passed",
                    "enabled": {
                        "finished_reason": enabled_round["finished_reason"],
                        "model_call_started": enabled_round["model_call_started"],
                        "reply_preview": enabled_round["text"][:200],
                    },
                    "tool": {
                        "finished_reason": tool_round["finished_reason"],
                        "tool_call_started": tool_round["tool_call_started"],
                        "reply_preview": tool_round["text"][:200],
                    },
                }
                evidence["checks"]["model_enabled_reply"] = True
                evidence["checks"]["model_called_selected_mcp_tool"] = True

            # 4) 运行 guard：启用放行 / 禁用拒绝
            allowed = await _authorize(
                ctrl.backend_url,
                ctrl.internal_token,
                employee["agent_id"],
                conversation["session_id"],
            )
            if allowed.status_code != 200:
                raise SmokeError(
                    f"guard 启用时应放行，实际 {allowed.status_code} "
                    f"{allowed.text}",
                )
            await client.patch(
                f"/api/employees/{employee['id']}",
                json={"enabled": False},
            )
            denied = await _authorize(
                ctrl.backend_url,
                ctrl.internal_token,
                employee["agent_id"],
                conversation["session_id"],
            )
            if denied.status_code != 403:
                raise SmokeError(
                    f"禁用后 guard 应拒绝，实际 {denied.status_code}",
                )
            # 直接绕过页面调用原生 /chat：必须未发生模型调用、无成功回复。
            guard_denied = await _run_guard_denied(
                adapter,
                employee["agent_id"],
                conversation["session_id"],
            )
            evidence["guard"] = {
                "enabled_authorize_status": allowed.status_code,
                "disabled_authorize_status": denied.status_code,
                "native_chat": guard_denied,
            }
            evidence["checks"]["disabled_guard_blocks_native_chat"] = True

            # 恢复启用，供重启后核对。
            reenabled = await client.patch(
                f"/api/employees/{employee['id']}",
                json={"enabled": True},
            )
            if reenabled.status_code != 200:
                raise SmokeError("重新启用失败")

        # 5) 两个真实进程重启后恢复（strict exit0 + generation 变化）
        ctrl.stop("nexus-backend", env)
        ctrl.stop("agentscope", env)
        ctrl.start("agentscope", env)
        ctrl.start("nexus-backend", env)
        await _wait_health(f"{ctrl.backend_url}/api/health", HEALTH_TIMEOUT)
        asc_after = await _wait_health(
            f"{ctrl.asc_url}/nexus/health",
            HEALTH_TIMEOUT,
        )
        generation_after = asc_after.get("generation_id")
        if not generation_before or generation_after == generation_before:
            raise SmokeError("重启后服务 generation_id 未变化，未证明真实重启")
        await adapter.aclose()
        adapter = AgentScopeHTTPClient(
            ctrl.asc_url,
            USER_ID,
            request_timeout=60.0,
            connect_timeout=10.0,
        )

        async with httpx.AsyncClient(
            base_url=ctrl.backend_url,
            headers={"X-Nexus-Owner": owner},
            timeout=60.0,
            trust_env=False,
        ) as client:
            detail = await client.get(f"/api/employees/{employee['id']}")
            if detail.status_code != 200:
                raise SmokeError("重启后员工未恢复")
            restored = detail.json()
            if restored["tools"] != [mcp_id] or restored["enabled"] is not True:
                raise SmokeError("重启后员工资源/启停未恢复")
            auth_after = await _authorize(
                ctrl.backend_url,
                ctrl.internal_token,
                employee["agent_id"],
                conversation["session_id"],
            )
            if auth_after.status_code != 200:
                raise SmokeError("重启后 guard 授权未恢复")
            views = await adapter.list_sessions(employee["agent_id"])
            session_ids = {v["session"]["id"] for v in views["sessions"]}
            if conversation["session_id"] not in session_ids:
                raise SmokeError("重启后原生 Session 未恢复")
            # 重启后实际重新发现所选 MCP：workpace 读回健康且仅含所选工具。
            mcps_after = await adapter.list_workspace_mcps(
                employee["agent_id"],
                conversation["session_id"],
            )
            installed_after = next(
                (m for m in mcps_after if m["name"] == MCP_NAME),
                None,
            )
            if installed_after is None or installed_after.get("is_healthy") is not True:
                raise SmokeError("重启后 workspace 未重新确认所选 MCP")
            tool_names_after = {t["name"] for t in installed_after["tools"]}
            if tool_names_after != {f"mcp__{MCP_NAME}__{SELECTED_TOOL}"}:
                raise SmokeError("重启后所选 MCP 工具集合不一致")
            evidence["restart"] = {
                "employee_restored": True,
                "authorize_status": auth_after.status_code,
                "session_restored": True,
                "mcp_rediscovered_healthy": True,
                "generation_before": generation_before,
                "generation_after": generation_after,
                "generation_changed": generation_after != generation_before,
            }
            evidence["checks"]["restart_recovery"] = True

            # 重启后真实再次调用所选工具（仅真实模型模式）。
            if with_model and key:
                tool_after = await _run_mcp_tool_round(
                    adapter,
                    employee["agent_id"],
                    conversation["session_id"],
                )
                evidence["restart"]["model_tool_after_restart"] = {
                    "finished_reason": tool_after["finished_reason"],
                    "tool_call_started": tool_after["tool_call_started"],
                    "reply_preview": tool_after["text"][:200],
                }
                evidence["checks"]["restart_model_calls_selected_tool"] = True

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        await adapter.aclose()
        for service in ("nexus-backend", "agentscope"):
            try:
                ctrl.stop(service, env)
            except SmokeError:
                pass
    return evidence


def _write_evidence(evidence: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-resources-{stamp}.json"
    evidence["evidence_path"] = str(out.relative_to(ROOT))
    evidence["key_recorded"] = False
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus 资源/会话/guard 冒烟")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help=(
            "追加真实模型启用轮与所选 MCP 真实工具轮（需 Root 注入 "
            "AGENTSCOPE_MODEL_API_KEY）。"
        ),
    )
    parser.add_argument(
        "--keep-evidence",
        action="store_true",
        help="保留本次临时数据目录用于排查。",
    )
    args = parser.parse_args(argv)

    key = ""
    if args.with_model:
        key = (os.environ.get("AGENTSCOPE_MODEL_API_KEY") or "").strip()
        if not key:
            print(
                "[未运行] --with-model 需要 AGENTSCOPE_MODEL_API_KEY；"
                "不 fallback 假模型。"
            )
            return 2

    data_root = Path(tempfile.mkdtemp(prefix="nexus-resources-"))
    asc_port = free_port()
    backend_port = free_port()
    while backend_port == asc_port:
        backend_port = free_port()
    ctrl = ServiceController(
        data_root=data_root,
        asc_port=asc_port,
        backend_port=backend_port,
    )
    evidence: dict
    try:
        # 独立启动的真实无状态 Streamable HTTP MCP 夹具（测试/冒烟专用，不是
        # 产品默认资源）；两个真实服务进程通过真实 HTTP 连接它。
        with running_http_mcp() as mcp_url:
            evidence = asyncio.run(
                _run(
                    ctrl,
                    mcp_url=mcp_url,
                    with_model=args.with_model,
                    key=key,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        evidence = {
            "mode": "full",
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
