#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nexus 员工管理真实进程冒烟（OpenSpec 任务 3.1）。

用交付的 ``scripts/services.py`` **实际启动**官方 AgentScope 服务与 Nexus
后端两个真实进程（隔离临时 data_dir/端口），通过后端真实 HTTP 接口完成员工
创建、列表/搜索、详情、编辑，并读取官方原生 Agent 记录证明：

* 创建后 AgentScope 中确有原生 Agent，顶层 id 与 ``data.id`` 不同；
* 原生 name 与编译后的中文 ``system_prompt`` 与业务字段一致；
* 编辑职责后原生 ``system_prompt`` 真实更新；
* 名称/领域忽略大小写重复返回明确冲突；校验错误返回明确字段错误；
* 同一名称并发创建只有一个成功，落败者创建的原生 Agent 被补偿删除；
* 上游停止后创建/读取返回明确的未确认/失败，不伪造成功。

``--with-model`` 需调用方安全注入 ``AGENTSCOPE_MODEL_API_KEY``：通过**业务
API PATCH** 把随机验证口令写入该员工工作指令（不直接覆盖原生 prompt），再
新建原生 Session 并用真实模型提问验证新会话使用更新后的职责/指令。未注入时
``model_check`` 明确标记 ``skipped``，只声明 CRUD 通过，不写整体模型通过，也
**不 fallback 假模型、不伪造回复**。

    uv run python scripts/smoke_employees.py                # 原生 CRUD 冒烟
    uv run python scripts/smoke_employees.py --with-model   # 追加真实模型校验

证据（脱敏，不含密钥）写入 ``evidence/phase3/``。
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
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))

from nexus_backend.adapter import AgentScopeHTTPClient  # noqa: E402
from smoke_runtime import (  # noqa: E402
    HEALTH_TIMEOUT,
    ServiceController,
    SmokeError,
    _safe_error,
    _wait_health,
    free_port,
    run_round,
)

USER_ID = "nexus-service"
EXPECTED_VERSION = "2.0.10"
EVIDENCE_DIR = ROOT / "evidence" / "phase3"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(**overrides) -> dict:
    payload = {
        "name": "星河",
        "domain": "测试自动化工程师",
        "responsibilities": "负责自动化脚本开发、框架维护与失败定位。",
        "instructions": "收到消息后先判断能否直接回答；信息不足时追问。",
        "avatar": "👩🏻‍💻",
    }
    payload.update(overrides)
    return payload


async def _native_agent(adapter, agent_id: str) -> dict | None:
    return await adapter.find_agent(agent_id)


async def _run_crud(client, adapter, evidence: dict) -> dict:
    created = (await client.post("/api/employees", json=_payload())).json()
    if not created.get("id"):
        raise SmokeError(f"创建员工失败：{created}")
    evidence["created"] = created

    matched = await _native_agent(adapter, created["agent_id"])
    if matched is None:
        raise SmokeError("创建后原生 Agent 不存在")
    prompt = matched["data"]["system_prompt"]
    evidence["native_after_create"] = {
        "id": matched["id"],
        "data_id": matched["data"]["id"],
        "two_ids_differ": matched["id"] != matched["data"]["id"],
        "name": matched["data"]["name"],
        "prompt_has_domain": "测试自动化工程师" in prompt,
        "prompt_has_responsibilities": (
            "负责自动化脚本开发、框架维护与失败定位。" in prompt
        ),
    }
    if matched["id"] == matched["data"]["id"]:
        raise SmokeError("原生顶层 id 与 data.id 相同，绑定语义异常")
    if matched["data"]["name"] != "星河":
        raise SmokeError("原生 name 与创建值不一致")
    if "测试自动化工程师" not in prompt:
        raise SmokeError("原生 system_prompt 未包含责任领域")
    if "负责自动化脚本开发、框架维护与失败定位。" not in prompt:
        raise SmokeError("原生 system_prompt 未包含职责说明")

    listed = (await client.get("/api/employees")).json()
    by_name = (
        await client.get("/api/employees", params={"q": "星河"})
    ).json()
    by_domain = (
        await client.get("/api/employees", params={"q": "自动化"})
    ).json()
    detail = (
        await client.get(f"/api/employees/{created['id']}")
    ).json()
    evidence["readback"] = {
        "list_total": listed["total"],
        "search_name_total": by_name["total"],
        "search_domain_total": by_domain["total"],
        "detail_name": detail["name"],
        "detail_enabled": detail["enabled"],
    }
    if listed["total"] != 1 or by_name["total"] != 1 or by_domain["total"] != 1:
        raise SmokeError("列表或搜索未返回真实记录")
    if detail["name"] != "星河" or detail["enabled"] is not True:
        raise SmokeError("详情与保存值不一致")

    patch = await client.patch(
        f"/api/employees/{created['id']}",
        json={"responsibilities": "改为负责性能压测与容量评估。"},
    )
    if patch.status_code != 200:
        raise SmokeError(f"编辑员工失败：{patch.status_code} {patch.text}")
    updated_native = await _native_agent(adapter, created["agent_id"])
    updated_prompt = updated_native["data"]["system_prompt"]
    evidence["native_after_patch"] = {
        "prompt_has_new_responsibilities": (
            "改为负责性能压测与容量评估。" in updated_prompt
        ),
        "prompt_dropped_old": "框架维护与失败定位" not in updated_prompt,
    }
    if "改为负责性能压测与容量评估。" not in updated_prompt:
        raise SmokeError("编辑后原生 system_prompt 未更新")
    return created


async def _run_errors(client, evidence: dict) -> None:
    dup_name = await client.post(
        "/api/employees",
        json=_payload(name="星河", domain="其他领域"),
    )
    dup_domain = await client.post(
        "/api/employees",
        json=_payload(name="知微", domain="测试自动化工程师"),
    )
    long_name = await client.post(
        "/api/employees", json=_payload(name="名" * 25)
    )
    bad_avatar = await client.post(
        "/api/employees", json=_payload(avatar="🐶")
    )
    evidence["errors"] = {
        "duplicate_name_status": dup_name.status_code,
        "duplicate_domain_status": dup_domain.status_code,
        "long_name_status": long_name.status_code,
        "bad_avatar_status": bad_avatar.status_code,
    }
    if dup_name.status_code != 409 or dup_domain.status_code != 409:
        raise SmokeError("重复名称/领域未返回 409")
    if long_name.status_code != 422 or bad_avatar.status_code != 422:
        raise SmokeError("校验错误未返回 422")


async def _run_concurrency(client, adapter, evidence: dict) -> None:
    before = (await adapter.list_agents())["total"]
    payload = _payload(name="并发员工", domain="并发领域")
    first, second = await asyncio.gather(
        client.post("/api/employees", json=payload),
        client.post("/api/employees", json=payload),
    )
    statuses = sorted([first.status_code, second.status_code])
    after = (await adapter.list_agents())["total"]
    evidence["concurrency"] = {
        "statuses": statuses,
        "native_before": before,
        "native_after": after,
        "native_delta": after - before,
    }
    if statuses != [201, 409]:
        raise SmokeError(f"并发创建状态异常：{statuses}")
    if after - before != 1:
        raise SmokeError("并发创建未正确补偿孤立原生 Agent")


async def _run_with_model(
    client,
    adapter,
    *,
    employee_id: str,
    agent_id: str,
    evidence: dict,
) -> None:
    """通过业务 API 更新职责/指令后，用真实模型证明新会话使用更新后的 prompt。

    验证值（随机口令）经**业务 PATCH** 写入工作指令，绝不直接覆盖原生 prompt；
    随后新建原生 Session 并真实提问，要求模型只回复该口令。每轮用已验证的
    ``run_round`` 流/终态校准，未拿到口令不得判定通过。
    """
    token = "ZQ" + secrets.token_hex(4)
    defaults = await adapter.service_defaults()
    if defaults.get("model_configured") is not True:
        raise SmokeError("/nexus/defaults 报告模型未配置")

    instructions = (
        "当用户要求验证口令时，只回复口令本身，不要有任何其他内容。"
        f"验证口令是：{token}"
    )
    patch = await client.patch(
        f"/api/employees/{employee_id}",
        json={"instructions": instructions},
    )
    if patch.status_code != 200:
        raise SmokeError(
            f"业务 PATCH 更新工作指令失败：{patch.status_code} {patch.text}"
        )
    # 证明业务 PATCH 已真实写入原生 prompt（而非直接覆盖原生）。
    native = await adapter.find_agent(agent_id)
    if native is None or token not in native["data"]["system_prompt"]:
        raise SmokeError("业务 PATCH 后原生 prompt 未包含验证口令")

    # 通过正式业务接口新建 Conversation（共同 Session factory），不绕过运行
    # guard 直接裸建原生 Session。
    conversation_resp = await client.post(
        "/api/conversations",
        json={
            "employee_id": employee_id,
            "client_key": uuid.uuid4().hex,
        },
    )
    if conversation_resp.status_code != 201:
        raise SmokeError(
            "新建会话失败："
            f"{conversation_resp.status_code} {conversation_resp.text}",
        )
    conversation = conversation_resp.json()
    if conversation.get("status") != "ready":
        raise SmokeError(f"会话未 ready：{conversation}")
    session_id = conversation["session_id"]
    if conversation["agent_id"] != agent_id:
        raise SmokeError("会话绑定的 Agent 与员工不一致")
    result = await run_round(
        adapter,
        agent_id,
        session_id,
        "请只回复验证口令本身。",
    )
    recalled = token.lower() in result["reply_text"].lower()
    evidence["model_check"] = {
        "status": "passed" if recalled else "failed",
        "token": token,
        "conversation_id": conversation["id"],
        "session_id": session_id,
        "reply_id": result["reply_id"],
        "finished_reason": result["finished_reason"],
        "event_types": result["event_types"],
        "reply_preview": result["reply_preview"],
        "recalled": recalled,
    }
    if not recalled:
        raise SmokeError("新 Session 未使用业务更新后的职责/指令")



async def _run(ctrl: ServiceController, with_model: bool, key: str) -> dict:
    evidence: dict = {
        "mode": "full",
        "run_at": _now(),
        "scope": "crud",
        "model_check": {
            "status": "skipped",
            "reason": (
                "未提供 AGENTSCOPE_MODEL_API_KEY，真实模型新会话校验未运行；"
                "本证据仅证明真实进程 CRUD，不代表模型行为。"
            ),
        },
        "checks": {},
    }
    env = ctrl.env_without_key()
    adapter = AgentScopeHTTPClient(
        ctrl.asc_url,
        USER_ID,
        request_timeout=30.0,
        connect_timeout=5.0,
    )
    agent_id: str | None = None
    employee_id: str | None = None
    try:
        ctrl.start("agentscope", env)
        ctrl.start("nexus-backend", env)
        backend_health = await _wait_health(
            f"{ctrl.backend_url}/api/health",
            HEALTH_TIMEOUT,
        )
        upstream = backend_health["agentscope"]
        evidence["agentscope"] = {
            "base_url": ctrl.asc_url,
            "version": upstream["version"],
            "reachable": upstream["reachable"],
        }
        evidence["backend"] = {
            "base_url": ctrl.backend_url,
            "status": backend_health["status"],
        }
        if upstream["version"] != EXPECTED_VERSION:
            raise SmokeError(f"版本不符：{upstream['version']}")
        evidence["checks"]["backend_health_ok"] = True

        async with httpx.AsyncClient(
            base_url=ctrl.backend_url,
            timeout=30.0,
            trust_env=False,
        ) as client:
            created = await _run_crud(client, adapter, evidence)
            agent_id = created["agent_id"]
            employee_id = created["id"]
            await _run_errors(client, evidence)
            await _run_concurrency(client, adapter, evidence)

            # 上游停止后：创建/读取必须明确失败，不伪造成功。
            ctrl.stop("agentscope", env)
            down_create = await client.post(
                "/api/employees",
                json=_payload(name="上游失败员工", domain="上游失败领域"),
            )
            down_list = await client.get("/api/employees")
            evidence["upstream_down"] = {
                "create_status": down_create.status_code,
                "list_status": down_list.status_code,
            }
            if down_create.status_code < 500 or down_list.status_code < 500:
                raise SmokeError("上游停止后未返回明确失败")
            evidence["checks"]["upstream_down_reports_failure"] = True

        evidence["crud_status"] = "passed"

        if with_model:
            # 重新拉起上游以执行真实模型校验；进程已重启，丢弃旧连接池。
            ctrl.start("agentscope", ctrl.env_with_key(key))
            await _wait_health(f"{ctrl.asc_url}/nexus/health", HEALTH_TIMEOUT)
            await adapter.aclose()
            adapter = AgentScopeHTTPClient(
                ctrl.asc_url,
                USER_ID,
                request_timeout=60.0,
                connect_timeout=10.0,
            )
            async with httpx.AsyncClient(
                base_url=ctrl.backend_url,
                headers={"X-Nexus-Owner": str(uuid.uuid4())},
                timeout=60.0,
                trust_env=False,
            ) as model_client:
                await _run_with_model(
                    model_client,
                    adapter,
                    employee_id=employee_id,
                    agent_id=agent_id,
                    evidence=evidence,
                )
            evidence["scope"] = "crud+model"
            evidence["checks"]["model_new_session_uses_updated_prompt"] = True

        evidence["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        evidence["status"] = "failed"
        evidence["error"] = _safe_error(exc)
    finally:
        if adapter is not None:
            if agent_id is not None:
                try:
                    await adapter.delete_agent(agent_id, ignore_missing=True)
                except Exception:  # noqa: BLE001
                    pass
            await adapter.aclose()
        try:
            ctrl.stop("nexus-backend", env)
        except SmokeError:
            pass
        try:
            ctrl.stop("agentscope", env)
        except SmokeError:
            pass
    return evidence


def _write_evidence(evidence: dict) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = EVIDENCE_DIR / f"smoke-employees-{stamp}.json"
    evidence["evidence_path"] = str(out.relative_to(ROOT))
    evidence["key_recorded"] = False
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus 员工管理真实进程冒烟")
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="追加真实模型新会话校验：经业务 PATCH 写入口令后真实提问（需注入密钥）。",
    )
    parser.add_argument(
        "--keep-evidence",
        action="store_true",
        help="保留本次临时数据目录用于排查。",
    )
    args = parser.parse_args(argv)

    load_dotenv(ROOT / ".env", override=False)
    key = (os.environ.get("AGENTSCOPE_MODEL_API_KEY") or "").strip()
    if args.with_model and not key:
        print(
            "[未运行] 需要 --with-model 时提供 AGENTSCOPE_MODEL_API_KEY；"
            "不 fallback 假模型。"
        )
        return 2

    data_root = Path(tempfile.mkdtemp(prefix="nexus-employees-"))
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
        evidence = asyncio.run(_run(ctrl, args.with_model, key))
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
    scope = evidence.get("scope", "crud")
    model_status = evidence.get("model_check", {}).get("status")
    print(
        f"[{status}] scope={scope} model_check={model_status} "
        f"证据已写入 {out}"
    )
    if not args.keep_evidence:
        shutil.rmtree(data_root, ignore_errors=True)
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
