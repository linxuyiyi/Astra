#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nexus 知识库真实本地检索冒烟（OpenSpec 任务 3.2.2）。

用交付的 ``scripts/services.py`` **实际启动/停止/重启**两个真实进程
（官方 AgentScope 服务 + Nexus 后端），对**外部只读**的本地 Ollama embedding
实例走完整原生 RAG 链路：

1. 通过官方 ``POST /credential/`` 引导 embedding 凭据、``POST /knowledge_bases/``
   建库、真实上传两份不同主题 TXT、轮询 ``documents/status`` 到 ``ready`` 且
   ``chunk_count>0``，再 ``POST .../search`` 拿到含文档独有随机值的 chunk。
2. 通过产品路径把真实 KB 关联到员工，用共同 Conversation factory 建新会话；
   核对 Nexus KB 目录投影、Employee 关联与原生 Session ``knowledge_config``
   （``mode=static``）全部对齐。
3. 运行 guard 严格边界（不需要真实 chat 模型）：
   * 空库检索（零结果成功）必须允许进入模型（出现 ``MODEL_CALL_START``）；
   * embedding 不可达（凭据指向未监听端口）、KB 被删除、两个所选 KB 之一不可
     构建，均须真实流 error 且**没有** ``MODEL_CALL_START``/成功回答。
4. 停止并重启 AgentScope（严格 exit 0、generation 变化）后同 KB/文档 chunk/
   Session 关联仍存在，并能再次真实检索。
5. ``--with-model``（需 Root 注入 ``AGENTSCOPE_MODEL_API_KEY``）时真实 SSE 含
   ``HINT_BLOCK`` 且回答包含文档独有值；未提供密钥时该段明确 ``skipped``，
   绝不 fallback 假模型。

隔离 data_dir/端口；Ollama 模型实例外部只读使用，绝不停止/删除该实例或其模型
目录。清理只删除本次创建的资源，不触碰默认 data。证据脱敏写入
``evidence/phase3/``。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
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

from nexus_backend.adapter import AgentScopeHTTPClient, UpstreamError  # noqa: E402
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
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11435"
CHAT_PARAMS = {"mode": "static", "top_k": 3, "emit_hint_event": True}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# 原生 Knowledge HTTP（官方公开路由；只做薄封装，不复制 RAG/CRUD）
# ----------------------------------------------------------------------
class KnowledgeClient:
    """通过官方公开 HTTP 路由读写知识库（内部租户身份，仅回环）。"""

    def __init__(self, base_url: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-User-ID": USER_ID},
            timeout=60.0,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upsert_ollama_credential(
        self,
        credential_id: str,
        host: str,
    ) -> None:
        response = await self._client.post(
            "/credential/",
            json={
                "data": {
                    "id": credential_id,
                    "name": credential_id,
                    "type": "ollama_credential",
                    "host": host,
                },
            },
        )
        if response.status_code != 201:
            raise SmokeError(
                f"引导 embedding 凭据失败：{response.status_code} "
                f"{response.text}",
            )

    async def create_knowledge_base(
        self,
        name: str,
        *,
        credential_id: str,
        model: str,
        dimensions: int,
    ) -> str:
        response = await self._client.post(
            "/knowledge_bases/",
            json={
                "name": name,
                "description": f"smoke kb {name}",
                "embedding_model_config": {
                    "type": "ollama_credential",
                    "credential_id": credential_id,
                    "model": model,
                    "dimensions": dimensions,
                    "parameters": {},
                },
                "chunker_config": {
                    "type": "approx_token",
                    "parameters": {"chunk_size": 512, "overlap": 50},
                },
            },
        )
        if response.status_code != 201:
            raise SmokeError(
                f"建库失败：{response.status_code} {response.text}",
            )
        return response.json()["knowledge_base_id"]

    async def delete_knowledge_base(self, kb_id: str) -> None:
        response = await self._client.delete(f"/knowledge_bases/{kb_id}")
        if response.status_code not in (204, 404):
            raise SmokeError(
                f"删除知识库失败：{response.status_code} {response.text}",
            )

    async def upload_document(
        self,
        kb_id: str,
        filename: str,
        text: str,
    ) -> str:
        response = await self._client.post(
            f"/knowledge_bases/{kb_id}/documents",
            files={"file": (filename, text.encode("utf-8"), "text/plain")},
            data={"content_type": "text/plain"},
        )
        if response.status_code != 201:
            raise SmokeError(
                f"上传文档失败：{response.status_code} {response.text}",
            )
        body = response.json()
        if body.get("status") != "pending":
            raise SmokeError(f"上传后状态非 pending：{body}")
        return body["document_id"]

    async def document_status(self, kb_id: str, document_id: str) -> dict:
        response = await self._client.get(
            f"/knowledge_bases/{kb_id}/documents/status",
            params={"ids": document_id},
        )
        if response.status_code != 200:
            raise SmokeError(
                f"查询文档状态失败：{response.status_code} {response.text}",
            )
        items = response.json().get("items", [])
        if not items:
            raise SmokeError("文档状态查询未返回条目")
        return items[0]

    async def wait_ready(
        self,
        kb_id: str,
        document_id: str,
        *,
        timeout: float = 120.0,
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            item = await self.document_status(kb_id, document_id)
            last = item
            status = item.get("status")
            if status == "error":
                raise SmokeError(
                    f"文档索引失败：{item.get('error')!r}",
                )
            if status == "ready" and int(item.get("chunk_count") or 0) > 0:
                return item
            await asyncio.sleep(0.5)
        raise SmokeError(f"文档索引超时：{last}")

    async def search(self, kb_id: str, query: str, top_k: int = 3) -> list:
        response = await self._client.post(
            f"/knowledge_bases/{kb_id}/search",
            json={"query": query, "top_k": top_k},
        )
        if response.status_code != 200:
            raise SmokeError(
                f"检索失败：{response.status_code} {response.text}",
            )
        return response.json().get("results", [])

    async def list_knowledge_bases(self) -> list:
        response = await self._client.get(
            "/knowledge_bases/",
            params={"page": 1, "page_size": 100},
        )
        if response.status_code != 200:
            raise SmokeError(
                f"知识库目录读取失败：{response.status_code}",
            )
        return response.json().get("knowledge_bases", [])


# ----------------------------------------------------------------------
# 运行事件收集
# ----------------------------------------------------------------------
async def _wait_idle(adapter, agent_id: str, session_id: str, timeout=30.0):
    deadline = time.monotonic() + timeout
    last: str | None = None
    while time.monotonic() < deadline:
        status = await adapter.get_session_status(agent_id, session_id)
        last = status.get("status")
        if last == "idle":
            return last
        await asyncio.sleep(0.5)
    return last


async def _run_round(
    adapter,
    agent_id: str,
    session_id: str,
    *,
    text: str,
    stop_at_model_start: bool = False,
    timeout: float = 60.0,
) -> dict:
    """触发一轮真实运行并收集事件；可选在首个 MODEL_CALL_START 处停下并中断。"""
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
    hint_seen = False
    stopped = False
    stream = adapter.stream_events(agent_id, session_id, after=cursor)
    try:
        async def _consume() -> None:
            nonlocal end_event, hint_seen, stopped
            async for event in stream:
                kind = event.get("type")
                event_types.append(kind)
                if kind == "TEXT_BLOCK_DELTA" and isinstance(
                    event.get("delta"),
                    str,
                ):
                    text_deltas.append(event["delta"])
                if kind == "HINT_BLOCK":
                    hint_seen = True
                if kind == "REPLY_END":
                    end_event = event
                    return
                if stop_at_model_start and kind == "MODEL_CALL_START":
                    stopped = True
                    return

        await asyncio.wait_for(_consume(), timeout=timeout)
    except (Exception, asyncio.TimeoutError):  # noqa: BLE001 - 断流仅记录
        pass
    finally:
        await stream.aclose()
    if stopped:
        # 主动中断仍在跑的占位密钥轮，避免等待远端 chat 失败。
        try:
            await adapter.interrupt_session(agent_id, session_id)
        except UpstreamError:
            pass
    idle_status = await _wait_idle(adapter, agent_id, session_id)
    if idle_status != "idle":
        raise SmokeError(
            f"会话未在超时内回到 idle（实际 {idle_status}），本轮未确认结束",
        )
    return {
        "trigger_status": trigger.get("status"),
        "event_types": event_types,
        "end_event": end_event,
        "finished_reason": (end_event or {}).get("finished_reason"),
        "text": "".join(text_deltas),
        "hint_block": hint_seen,
        "model_call_started": "MODEL_CALL_START" in event_types,
        "stopped_at_model_start": stopped,
    }


async def _authorize_status(
    backend_url: str,
    token: str,
    agent_id: str,
    session_id: str,
) -> int:
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
    return response.status_code


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
async def _create_employee(client, owner_headers, name, knowledge):
    response = await client.post(
        "/api/employees",
        headers=owner_headers,
        json={
            "name": name,
            "domain": f"知识验证域{uuid.uuid4().hex[:4]}",
            "responsibilities": (
                "请只依据你检索到的资料回答用户问题；资料中没有就说不知道。"
            ),
            "instructions": "回答简洁，只回答被要求的内容。",
            "avatar": "🤖",
            "knowledge": knowledge,
        },
    )
    if response.status_code != 201:
        raise SmokeError(
            f"创建员工失败：{response.status_code} {response.text}",
        )
    return response.json()


async def _create_conversation(client, owner_headers, employee_id):
    response = await client.post(
        "/api/conversations",
        headers=owner_headers,
        json={"employee_id": employee_id, "client_key": uuid.uuid4().hex},
    )
    if response.status_code != 201:
        raise SmokeError(
            f"创建会话失败：{response.status_code} {response.text}",
        )
    conversation = response.json()
    if conversation["status"] != "ready":
        raise SmokeError(f"会话未 ready：{conversation}")
    return conversation


async def _run(
    ctrl: ServiceController,
    *,
    with_model: bool,
    key: str,
    ollama_host: str,
) -> dict:
    evidence: dict = {"mode": "full", "run_at": _now(), "checks": {}}
    env = ctrl.base_env()
    env["AGENTSCOPE_KNOWLEDGE_ENABLED"] = "true"
    env["AGENTSCOPE_EMBEDDING_HOST"] = ollama_host
    if with_model and key:
        env["AGENTSCOPE_MODEL_API_KEY"] = key
    else:
        env.setdefault("AGENTSCOPE_MODEL_API_KEY", "nexus-smoke-placeholder")
    if not with_model:
        evidence["model_check"] = {
            "status": "skipped",
            "reason": (
                "未提供 AGENTSCOPE_MODEL_API_KEY，真实模型回答与 Knowledge hint "
                "未运行；本证据不声称真实 RAG 回答已完成。"
            ),
        }

    adapter = AgentScopeHTTPClient(
        ctrl.asc_url,
        USER_ID,
        request_timeout=60.0,
        connect_timeout=10.0,
    )
    know = KnowledgeClient(ctrl.asc_url)
    owner = str(uuid.uuid4())
    owner_headers = {"X-Nexus-Owner": owner}
    created_kbs: list[str] = []
    created_agents: list[str] = []
    secret = "ZS" + secrets.token_hex(4)
    target_topic = "星云计划"
    other_topic = "珊瑚礁"

    try:
        ctrl.start("agentscope", env)
        ctrl.start("nexus-backend", env)
        backend_health = await _wait_health(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        if backend_health["agentscope"]["version"] != EXPECTED_VERSION:
            raise SmokeError("AgentScope 版本不符")
        asc_health = await _wait_health(
            f"{ctrl.asc_url}/nexus/health",
            HEALTH_TIMEOUT,
        )
        generation_before = asc_health.get("generation_id")
        if asc_health.get("knowledge_enabled") is not True:
            raise SmokeError("知识库未启用")
        if asc_health.get("embedding_configured") is not True:
            raise SmokeError("embedding 凭据未引导")
        embed_credential = asc_health.get("embedding_credential_id")
        embed_model = asc_health.get("embedding_model")
        embed_dims = asc_health.get("embedding_dimensions")
        evidence["embedding"] = {
            "credential_id": embed_credential,
            "model": embed_model,
            "dimensions": embed_dims,
            "host": ollama_host,
        }
        evidence["checks"]["knowledge_enabled_and_embedding_ready"] = True

        # 1) 两个不同主题 KB + 真实 TXT 上传/索引/检索
        kb_target = await know.create_knowledge_base(
            f"smoke-target-{uuid.uuid4().hex[:6]}",
            credential_id=embed_credential,
            model=embed_model,
            dimensions=embed_dims,
        )
        kb_other = await know.create_knowledge_base(
            f"smoke-other-{uuid.uuid4().hex[:6]}",
            credential_id=embed_credential,
            model=embed_model,
            dimensions=embed_dims,
        )
        kb_empty = await know.create_knowledge_base(
            f"smoke-empty-{uuid.uuid4().hex[:6]}",
            credential_id=embed_credential,
            model=embed_model,
            dimensions=embed_dims,
        )
        created_kbs.extend([kb_target, kb_other, kb_empty])

        doc_target = await know.upload_document(
            kb_target,
            "xingyun.txt",
            (
                f"{target_topic}的内部对接编号是 {secret}，仅限内部资料使用。"
                f"{target_topic}由研发中心负责推进，预计分三个阶段交付。"
            ),
        )
        doc_other = await know.upload_document(
            kb_other,
            "coral.txt",
            (
                f"{other_topic}生态研究关注海水温度与珊瑚白化之间的关系，"
                "与星云计划无关。"
            ),
        )
        target_status = await know.wait_ready(kb_target, doc_target)
        other_status = await know.wait_ready(kb_other, doc_other)
        evidence["documents"] = {
            "target": {
                "id": doc_target,
                "status": target_status["status"],
                "chunk_count": target_status["chunk_count"],
            },
            "other": {
                "id": doc_other,
                "status": other_status["status"],
                "chunk_count": other_status["chunk_count"],
            },
            "verification_value": secret,
        }
        evidence["checks"]["real_index_ready"] = True

        # 真实检索目标 KB，chunk 必须含文档独有随机值。
        results = await know.search(
            kb_target,
            f"{target_topic}的对接编号是什么？",
            top_k=3,
        )
        joined = json.dumps(results, ensure_ascii=False)
        if secret not in joined:
            raise SmokeError("真实检索未返回含文档独有值的 chunk")
        evidence["search"] = {
            "result_count": len(results),
            "contains_value": True,
        }
        evidence["checks"]["real_search_hits_value"] = True

        # 空库检索真实返回 0 结果（不是故障）。
        empty_results = await know.search(kb_empty, "任意问题", top_k=3)
        if empty_results != []:
            raise SmokeError("空知识库检索应真实返回 0 结果")
        evidence["checks"]["empty_kb_search_zero_success"] = True

        # 2) 员工关联 + 共同 Conversation factory + 原生配置对齐
        async with httpx.AsyncClient(
            base_url=ctrl.backend_url,
            headers=owner_headers,
            timeout=60.0,
            trust_env=False,
        ) as client:
            catalog = (
                await client.get("/api/resources/knowledge")
            ).json()
            listed_ids = {item["id"] for item in catalog["knowledge"]["items"]}
            if not {kb_target, kb_other, kb_empty} <= listed_ids:
                raise SmokeError("Nexus 知识库目录未列出本次创建的 KB")
            evidence["checks"]["nexus_catalog_lists_kbs"] = True

            employee = await _create_employee(
                client,
                owner_headers,
                f"知识员工{uuid.uuid4().hex[:6]}",
                [kb_target, kb_other],
            )
            if employee.get("agent_id"):
                created_agents.append(employee["agent_id"])
            if employee["knowledge"] != [kb_target, kb_other]:
                raise SmokeError("员工 Knowledge 默认关联未真实保存")
            conversation = await _create_conversation(
                client,
                owner_headers,
                employee["id"],
            )
            snapshot = conversation["capability_snapshot"].get("knowledge")
            if snapshot != [kb_target, kb_other]:
                raise SmokeError("会话能力快照未记录所选 KB")
            # 原生 Session 配置必须与所选 KB 及 static 参数一致。
            views = await adapter.list_sessions(employee["agent_id"])
            session = next(
                v["session"]
                for v in views["sessions"]
                if v["session"]["id"] == conversation["session_id"]
            )
            native_knowledge = session["config"]["knowledge_config"]
            if set(native_knowledge.get("knowledge_base_ids", [])) != {
                kb_target,
                kb_other,
            }:
                raise SmokeError("原生 Session 的 Knowledge 关联不一致")
            if native_knowledge.get("parameters") != CHAT_PARAMS:
                raise SmokeError(
                    f"原生 Session 未使用 static RAG 参数："
                    f"{native_knowledge.get('parameters')}",
                )
            evidence["conversation"] = {
                "id": conversation["id"],
                "session_id": conversation["session_id"],
                "agent_id": conversation["agent_id"],
                "knowledge_snapshot": snapshot,
                "native_knowledge_config": native_knowledge,
            }
            evidence["checks"]["session_knowledge_aligned"] = True

            # 目标库员工（仅选含文档的目标 KB）：供真实模型轮使用，避免受后续
            # 删除其他 KB 的影响。
            target_employee = await _create_employee(
                client,
                owner_headers,
                f"目标员工{uuid.uuid4().hex[:6]}",
                [kb_target],
            )
            if target_employee.get("agent_id"):
                created_agents.append(target_employee["agent_id"])
            target_conversation = await _create_conversation(
                client,
                owner_headers,
                target_employee["id"],
            )

            # 2a) 空库员工：零结果成功应允许进入模型（占位密钥下 MODEL_CALL_START）。
            empty_employee = await _create_employee(
                client,
                owner_headers,
                f"空库员工{uuid.uuid4().hex[:6]}",
                [kb_empty],
            )
            if empty_employee.get("agent_id"):
                created_agents.append(empty_employee["agent_id"])
            empty_conversation = await _create_conversation(
                client,
                owner_headers,
                empty_employee["id"],
            )
            zero_round = await _run_round(
                adapter,
                empty_employee["agent_id"],
                empty_conversation["session_id"],
                text="请只回复：空库探针。",
                stop_at_model_start=True,
            )
            if not zero_round["model_call_started"]:
                raise SmokeError("空库零结果检索被误判为故障（未进入模型）")
            evidence["zero_result_probe"] = {
                "model_call_started": zero_round["model_call_started"],
            }
            evidence["checks"]["empty_search_not_failure"] = True

            # 3) 失败边界（无需真实 chat 模型）
            # 3a) embedding 不可达：凭据 host 指向未监听端口。
            bad_port = free_port()
            bad_host = f"http://127.0.0.1:{bad_port}"
            bad_credential = f"smoke-bad-{uuid.uuid4().hex[:6]}"
            await know.upsert_ollama_credential(bad_credential, bad_host)
            kb_bad = await know.create_knowledge_base(
                f"smoke-bad-{uuid.uuid4().hex[:6]}",
                credential_id=bad_credential,
                model=embed_model,
                dimensions=embed_dims,
            )
            created_kbs.append(kb_bad)
            bad_employee = await _create_employee(
                client,
                owner_headers,
                f"坏库员工{uuid.uuid4().hex[:6]}",
                [kb_bad],
            )
            if bad_employee.get("agent_id"):
                created_agents.append(bad_employee["agent_id"])
            bad_conversation = await _create_conversation(
                client,
                owner_headers,
                bad_employee["id"],
            )
            bad_round = await _run_round(
                adapter,
                bad_employee["agent_id"],
                bad_conversation["session_id"],
                text="请只依据资料回答。",
            )
            if bad_round["model_call_started"]:
                raise SmokeError("embedding 不可达时仍进入模型调用")
            if bad_round["finished_reason"] != "error":
                raise SmokeError(
                    f"embedding 不可达未真实失败：{bad_round['finished_reason']}",
                )
            evidence["embedding_unreachable"] = {
                "finished_reason": bad_round["finished_reason"],
                "model_call_started": bad_round["model_call_started"],
            }
            evidence["checks"]["embedding_unreachable_aborts"] = True

            # 3b) 两个所选 KB 之一不可构建：好 KB 成功、坏 KB 失败 → 中止。
            partial_employee = await _create_employee(
                client,
                owner_headers,
                f"混合员工{uuid.uuid4().hex[:6]}",
                [kb_target, kb_bad],
            )
            if partial_employee.get("agent_id"):
                created_agents.append(partial_employee["agent_id"])
            partial_conversation = await _create_conversation(
                client,
                owner_headers,
                partial_employee["id"],
            )
            partial_round = await _run_round(
                adapter,
                partial_employee["agent_id"],
                partial_conversation["session_id"],
                text="请只依据资料回答。",
            )
            if partial_round["model_call_started"]:
                raise SmokeError("部分 KB 不可用时仍进入模型调用")
            if partial_round["finished_reason"] != "error":
                raise SmokeError("部分 KB 不可用未真实失败")
            evidence["partial_kb_failure"] = {
                "finished_reason": partial_round["finished_reason"],
                "model_call_started": partial_round["model_call_started"],
            }
            evidence["checks"]["partial_kb_failure_aborts"] = True

            # 3c) KB 被删除：授权/运行必须拒绝。
            await know.delete_knowledge_base(kb_other)
            created_kbs.remove(kb_other)
            deleted_auth = await _authorize_status(
                ctrl.backend_url,
                ctrl.internal_token,
                employee["agent_id"],
                conversation["session_id"],
            )
            if deleted_auth != 403:
                raise SmokeError(
                    f"KB 删除后授权应拒绝，实际 {deleted_auth}",
                )
            deleted_round = await _run_round(
                adapter,
                employee["agent_id"],
                conversation["session_id"],
                text="请只依据资料回答。",
            )
            if deleted_round["model_call_started"]:
                raise SmokeError("KB 删除后仍进入模型调用")
            if deleted_round["finished_reason"] != "error":
                raise SmokeError("KB 删除后未真实失败")
            evidence["kb_deleted"] = {
                "authorize_status": deleted_auth,
                "finished_reason": deleted_round["finished_reason"],
            }
            evidence["checks"]["deleted_kb_aborts"] = True

            # 4) 真实模型轮（可选）
            if with_model and key:
                real = await _run_round(
                    adapter,
                    target_employee["agent_id"],
                    target_conversation["session_id"],
                    text=(
                        f"请依据知识库回答：{target_topic}的对接编号是什么？"
                        "如果资料里没有就说不知道。"
                    ),
                    timeout=180.0,
                )
                if not real["model_call_started"]:
                    raise SmokeError("真实模型轮未发生 MODEL_CALL_START")
                if not real["hint_block"]:
                    raise SmokeError("真实 SSE 未包含 Knowledge HINT_BLOCK")
                if real["finished_reason"] != "completed":
                    raise SmokeError(
                        f"真实模型轮未完成：{real['finished_reason']}",
                    )
                if secret not in real["text"]:
                    raise SmokeError("真实回答未包含文档独有值")
                evidence["model_check"] = {
                    "status": "passed",
                    "hint_block": real["hint_block"],
                    "finished_reason": real["finished_reason"],
                    "reply_preview": real["text"][:200],
                }
                evidence["checks"]["model_answers_with_doc_value"] = True

            # 5) 重启：严格 exit0 + generation 变化 + 数据恢复 + 再次检索
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
                raise SmokeError("重启后 generation_id 未变化，未证明真实重启")
            await adapter.aclose()
            adapter = AgentScopeHTTPClient(
                ctrl.asc_url,
                USER_ID,
                request_timeout=60.0,
                connect_timeout=10.0,
            )

            kb_ids_after = {kb["id"] for kb in await know.list_knowledge_bases()}
            if kb_target not in kb_ids_after:
                raise SmokeError("重启后知识库未恢复")
            status_after = await know.document_status(kb_target, doc_target)
            if status_after["status"] != "ready" or int(
                status_after.get("chunk_count") or 0,
            ) <= 0:
                raise SmokeError("重启后文档 chunk 未恢复")
            results_after = await know.search(
                kb_target,
                f"{target_topic}的对接编号是什么？",
                top_k=3,
            )
            if secret not in json.dumps(results_after, ensure_ascii=False):
                raise SmokeError("重启后真实检索未恢复命中")
            views_after = await adapter.list_sessions(target_employee["agent_id"])
            if target_conversation["session_id"] not in {
                v["session"]["id"] for v in views_after["sessions"]
            }:
                raise SmokeError("重启后原生 Session 未恢复")
            target_auth = await _authorize_status(
                ctrl.backend_url,
                ctrl.internal_token,
                target_employee["agent_id"],
                target_conversation["session_id"],
            )
            if target_auth != 200:
                raise SmokeError("重启后目标员工授权未恢复")
            # 被删 KB 的会话授权应依然拒绝（数据一致）。
            auth_after = await _authorize_status(
                ctrl.backend_url,
                ctrl.internal_token,
                employee["agent_id"],
                conversation["session_id"],
            )
            empty_auth = await _authorize_status(
                ctrl.backend_url,
                ctrl.internal_token,
                empty_employee["agent_id"],
                empty_conversation["session_id"],
            )
            if empty_auth != 200:
                raise SmokeError("重启后空库员工授权未恢复")
            evidence["restart"] = {
                "generation_before": generation_before,
                "generation_after": generation_after,
                "generation_changed": generation_after != generation_before,
                "kb_restored": True,
                "document_ready": status_after["status"],
                "chunk_count": status_after["chunk_count"],
                "search_restored": True,
                "session_restored": True,
                "target_authorize_status": target_auth,
                "deleted_kb_authorize_status": auth_after,
                "empty_employee_authorize_status": empty_auth,
            }
            evidence["checks"]["restart_recovery"] = True

            # 重启后再次真实运行本轮（零结果成功路径）：确认运行链路恢复。
            zero_round_after = await _run_round(
                adapter,
                empty_employee["agent_id"],
                empty_conversation["session_id"],
                text="请只回复：重启空库探针。",
                stop_at_model_start=True,
            )
            if not zero_round_after["model_call_started"]:
                raise SmokeError("重启后空库检索未进入模型，运行链路未恢复")
            evidence["restart"]["zero_result_probe_model_call_started"] = (
                zero_round_after["model_call_started"]
            )

            if with_model and key:
                real_after = await _run_round(
                    adapter,
                    target_employee["agent_id"],
                    target_conversation["session_id"],
                    text=(
                        f"请依据知识库回答：{target_topic}的对接编号是什么？"
                        "如果资料里没有就说不知道。"
                    ),
                    timeout=180.0,
                )
                if not real_after["model_call_started"] or secret not in (
                    real_after["text"]
                ):
                    raise SmokeError("重启后真实模型回答未包含文档独有值")
                evidence["restart"]["model_after_restart"] = {
                    "hint_block": real_after["hint_block"],
                    "finished_reason": real_after["finished_reason"],
                }
                evidence["checks"]["restart_model_answers"] = True

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        # 只清理本次创建的资源；外部 Ollama 实例只读使用、绝不触碰。
        for kb_id in created_kbs:
            try:
                await know.delete_knowledge_base(kb_id)
            except Exception:  # noqa: BLE001
                pass
        for agent_id in created_agents:
            try:
                await adapter.delete_agent(agent_id, ignore_missing=True)
            except Exception:  # noqa: BLE001
                pass
        await adapter.aclose()
        await know.aclose()
        for service in ("nexus-backend", "agentscope"):
            try:
                ctrl.stop(service, env)
            except SmokeError:
                pass
    return evidence


def _write_evidence(evidence: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-knowledge-{stamp}.json"
    evidence["evidence_path"] = str(out.relative_to(ROOT))
    evidence["key_recorded"] = False
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus 知识库真实检索冒烟")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="追加真实模型 Knowledge 回答轮（需注入 AGENTSCOPE_MODEL_API_KEY）。",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get(
            "NEXUS_SMOKE_OLLAMA_HOST",
            DEFAULT_OLLAMA_HOST,
        ),
        help="外部只读 Ollama 实例地址（默认 127.0.0.1:11435）。",
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
                "不 fallback 假模型。",
            )
            return 2

    data_root = Path(tempfile.mkdtemp(prefix="nexus-knowledge-"))
    asc_port = free_port()
    backend_port = free_port()
    while backend_port == asc_port:
        backend_port = free_port()
    ctrl = ServiceController(
        data_root=data_root,
        asc_port=asc_port,
        backend_port=backend_port,
    )
    try:
        evidence = asyncio.run(
            _run(
                ctrl,
                with_model=args.with_model,
                key=key,
                ollama_host=args.ollama_host.rstrip("/"),
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
