"""Worker 期望配置与实时运行观测的单一就绪判定事实源。

设计边界：
1. ``OpenCodeExecutionEnvironment`` 只存管理员维护的期望配置（Desired）。
2. ``WorkerInstance`` 只存网关心跳与当前连接代数（epoch）下的实时观测（Observed）。
3. ``evaluate_worker_readiness`` 是无副作用纯函数，统一驱动列表展示、详情健康度与任务调度门禁。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.domain import EnvironmentStatus, ExecutionMode, MaintenanceState, WorkerReadiness
from app.gateway.models import GatewayRoute
from app.models import (
    CodeRepository,
    GitCredential,
    OpenCodeExecutionEnvironment,
    WorkerInstance,
    WorkerMaintenanceOperation,
)
from app.runtime.opencode_model import route_model_id
from app.runtime.redaction import redact_data, redact_text
from app.runtime.repository import collect_skill_files
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_agent.inspection import skill_content_hash

DEFAULT_MCP = "nexus-library"
GATEWAY_PROVIDER = "nexus-gateway"
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"
ACTIVE_MAINTENANCE_STATES = frozenset({MaintenanceState.QUEUED.value, MaintenanceState.RUNNING.value})


@dataclass(slots=True)
class ReadinessLookupContext:
    """批量预加载的期望配置依赖，避免员工列表产生 N+1 数据库或磁盘读取。"""

    ordered_chat_routes: list[str] = field(default_factory=list)
    enabled_chat_routes: set[str] = field(default_factory=set)
    repositories: dict[str, CodeRepository] = field(default_factory=dict)
    credential_ciphertext_by_repo: dict[str, str] = field(default_factory=dict)
    skill_bodies: dict[str, str] = field(default_factory=dict)


def compute_target_digest(environment: OpenCodeExecutionEnvironment) -> str:
    """计算目标主机与服务绑定指纹。展示字段与模型/Skill/MCP 变更不影响该指纹。"""

    mode = str(environment.mode or ExecutionMode.LOCAL.value).strip().upper()
    workspace = str(environment.workspace_root or "").strip().rstrip("/")
    port = int(environment.opencode_server_port or 0)
    if mode == ExecutionMode.LOCAL.value:
        normalized = Path(workspace or ".").as_posix().rstrip("/")
        raw = f"LOCAL|{normalized}|{port}"
    else:
        host = str(environment.host or "").strip().lower()
        ssh_port = int(environment.port or 22)
        username = str(environment.username or "").strip()
        raw = f"LINUX_REMOTE|{host}|{ssh_port}|{username}|{workspace}|{port}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def capability_spec_revision(
    skill_refs: Iterable[str] | None,
    mcp_refs: Iterable[dict[str, Any]] | None,
) -> str:
    """纯函数：根据期望 Skill 与 MCP 绑定生成确定性配置修订号（供任务快照审计）。"""

    skills = sorted({str(name).strip() for name in (skill_refs or ()) if str(name).strip()})
    mcps: list[dict[str, Any]] = []
    for item in mcp_refs or ():
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        config = item.get("config")
        mcps.append(
            {
                "name": name,
                "url": sanitize_url(item.get("url")),
                "config": config if isinstance(config, dict) else {},
            }
        )
    mcps.sort(key=lambda row: (row["name"], row["url"]))
    if not skills and not mcps:
        return ""
    return _revision({"skills": skills, "mcp": mcps})


def mcp_endpoints_revision(items: Iterable[dict[str, Any]]) -> str:
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        rows.append({"name": name, "url": sanitize_url(item.get("url"))})
    rows.sort(key=lambda row: (row["name"], row["url"]))
    return _revision(rows) if rows else ""


async def load_readiness_context(
    session: AsyncSession,
    environments: Iterable[OpenCodeExecutionEnvironment],
    settings: Settings | None = None,
) -> ReadinessLookupContext:
    resolved = settings or get_settings()
    env_list = [env for env in environments if env is not None]
    ordered_routes = [
        str(model)
        for model in (
            await session.scalars(
                select(GatewayRoute.model)
                .where(GatewayRoute.enabled.is_(True), GatewayRoute.model_type == "CHAT")
                .order_by(GatewayRoute.model)
            )
        ).all()
    ]
    repo_ids = sorted({_text(env.repository_id) for env in env_list if _text(env.repository_id)})
    repositories: dict[str, CodeRepository] = {}
    credentials: dict[str, str] = {}
    if repo_ids:
        repo_rows = (
            await session.scalars(select(CodeRepository).where(CodeRepository.id.in_(repo_ids)))
        ).all()
        repositories = {row.id: row for row in repo_rows}
        cred_rows = (
            await session.scalars(
                select(GitCredential).where(GitCredential.repository_id.in_(repo_ids))
            )
        ).all()
        credentials = {
            row.repository_id: str(row.token_ciphertext)
            for row in cred_rows
            if row.token_ciphertext
        }

    skill_names = sorted(
        {
            str(name).strip()
            for env in env_list
            for name in (env.skill_refs or [])
            if str(name).strip()
        }
    )
    skill_bodies: dict[str, str] = {}
    if skill_names:
        source_dir = Path(resolved.workspace_root) / "worker-skills"
        skill_bodies = {
            item["name"]: item["body"] for item in collect_skill_files(skill_names, source_dir)
        }

    return ReadinessLookupContext(
        ordered_chat_routes=ordered_routes,
        enabled_chat_routes=set(ordered_routes),
        repositories=repositories,
        credential_ciphertext_by_repo=credentials,
        skill_bodies=skill_bodies,
    )


def build_desired_spec_from_context(
    environment: OpenCodeExecutionEnvironment,
    context: ReadinessLookupContext,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    payload: dict[str, Any] = {
        "target_digest": compute_target_digest(environment),
        "opencode": {"version": str(resolved.remote_opencode_version)},
        "worker_agent": {"version": WORKER_AGENT_VERSION},
        "model": _desired_model(environment, context),
        "repository": _desired_repository(environment, context),
        "skills": _desired_skills(environment, context),
        "mcp": _desired_mcp(environment, resolved),
    }
    payload["revision"] = _revision(
        {key: value for key, value in payload.items() if key != "target_digest"}
    )
    return payload


async def build_desired_spec(
    session: AsyncSession,
    environment: OpenCodeExecutionEnvironment,
    settings: Settings | None = None,
) -> dict[str, Any]:
    resolved = settings or get_settings()
    context = await load_readiness_context(session, [environment], resolved)
    return build_desired_spec_from_context(environment, context, resolved)


async def scheduling_block(
    session: AsyncSession,
    environment: OpenCodeExecutionEnvironment | None,
    settings: Settings | None = None,
) -> str | None:
    """返回首个不可调度阻断原因；可调度时返回 None。与列表/详情共享同一判定函数。"""

    if environment is None:
        return "员工尚未配置运行环境"
    from app.models import AgentWorker

    employee = (
        await session.get(AgentWorker, environment.employee_id)
        if environment.employee_id
        else None
    )
    if employee is None or not employee.enabled:
        return "员工已停用调度"
    active_op = None
    instance = None
    if environment.id is not None:
        active_op = await session.scalar(
            select(WorkerMaintenanceOperation).where(
                WorkerMaintenanceOperation.environment_id == environment.id,
                WorkerMaintenanceOperation.state.in_(tuple(ACTIVE_MAINTENANCE_STATES)),
            )
        )
        instance = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )
    desired = await build_desired_spec(session, environment, settings)
    evaluated = evaluate_worker_readiness(
        desired,
        environment,
        instance,
        employee_enabled=bool(employee.enabled),
        latest_operation=active_op,
    )
    blockers = evaluated.get("blocking") or []
    return str(blockers[0]["message"]) if blockers else None


def evaluate_worker_readiness(
    desired: dict[str, Any],
    environment: OpenCodeExecutionEnvironment,
    worker_instance: WorkerInstance | None = None,
    *,
    employee_enabled: bool = True,
    occupying_task_ids: Sequence[str] = (),
    active_session_ids: Sequence[str] = (),
    latest_operation: WorkerMaintenanceOperation | None = None,
) -> dict[str, Any]:
    """纯函数：根据期望配置与当前连接/观测快照计算唯一就绪状态与阻断原因。"""

    is_local = str(environment.mode) == ExecutionMode.LOCAL.value
    want_digest = _text(desired.get("target_digest")) or compute_target_digest(environment)
    blockers: list[dict[str, Any]] = []

    if not employee_enabled:
        blockers.append({"code": "disabled", "message": "员工已停用调度"})

    if latest_operation is not None and latest_operation.state in ACTIVE_MAINTENANCE_STATES:
        blockers.append({"code": "maintenance_active", "message": "环境正在执行维护操作"})

    if is_local:
        return _evaluate_local_readiness(
            desired=desired,
            environment=environment,
            want_digest=want_digest,
            employee_enabled=employee_enabled,
            occupying_task_ids=occupying_task_ids,
            active_session_ids=active_session_ids,
            latest_operation=latest_operation,
            base_blockers=blockers,
        )

    online = worker_instance is not None and worker_instance.status == "online"
    target_matched = bool(
        worker_instance is not None
        and _text(worker_instance.target_digest)
        and _text(worker_instance.target_digest) == want_digest
    )
    snapshot = (
        worker_instance.observed_snapshot
        if (
            worker_instance is not None
            and target_matched
            and isinstance(worker_instance.observed_snapshot, dict)
        )
        else None
    )
    observation = _normalize_snapshot(snapshot, worker_instance) if snapshot is not None else None
    rows = build_equipment_rows(desired, observation)
    repo_summary = build_repository_summary(desired, observation)
    opencode_last_error = _extract_opencode_last_error(observation)

    if worker_instance is None:
        readiness = WorkerReadiness.UNKNOWN.value
        blockers.append({"code": "worker_not_enrolled", "message": "远程环境尚未完成一键部署"})
    elif not target_matched:
        readiness = WorkerReadiness.DRIFTED.value
        blockers.append(
            {
                "code": "target_changed",
                "message": "目标服务器或路径已变更，请重新执行一键部署",
            }
        )
    elif not online:
        readiness = (
            WorkerReadiness.DEGRADED.value
            if observation is not None
            else WorkerReadiness.UNKNOWN.value
        )
        blockers.append({"code": "worker_offline", "message": "Worker 通道离线"})
    elif observation is None:
        readiness = WorkerReadiness.UNKNOWN.value
        blockers.append({"code": "snapshot_missing", "message": "等待节点上报运行快照"})
    else:
        row_states = [str(row.get("state") or "unknown") for row in rows if row.get("configured")]
        if observation.get("error") or any(state == "degraded" for state in row_states):
            readiness = WorkerReadiness.DEGRADED.value
            degraded_labels = [
                str(row.get("label") or row.get("key"))
                for row in rows
                if row.get("configured") and row.get("state") == "degraded"
            ]
            detail = f"运行异常：{'、'.join(degraded_labels)}" if degraded_labels else str(
                observation.get("error") or "节点运行自检异常"
            )
            if opencode_last_error and "OpenCode" in degraded_labels:
                first_line = opencode_last_error.splitlines()[-1][:160]
                detail = f"{detail}（{first_line}）"
            blockers.append({"code": "runtime_degraded", "message": redact_text(detail)})
        elif (
            any(state == "drifted" for state in row_states)
            or not desired.get("repository")
            or observation.get("spec_revision") != desired.get("revision")
        ):
            readiness = WorkerReadiness.DRIFTED.value
            if not desired.get("repository"):
                blockers.append({"code": "repository_missing", "message": "尚未绑定 Git 仓库"})
            else:
                drifted_labels = [
                    str(row.get("label") or row.get("key"))
                    for row in rows
                    if row.get("configured") and row.get("state") == "drifted"
                ]
                if not drifted_labels:
                    drifted_labels = ["运行配置绑定"]
                blockers.append(
                    {
                        "code": "config_drifted",
                        "message": f"配置已变更（{'、'.join(drifted_labels)}），待执行一键部署同步",
                    }
                )
        elif any(state == "unknown" for state in row_states):
            readiness = WorkerReadiness.UNKNOWN.value
            blockers.append({"code": "observation_incomplete", "message": "节点运行快照尚未完整核验"})
        else:
            readiness = WorkerReadiness.READY.value

    _append_occupancy_blockers(blockers, occupying_task_ids, active_session_ids)
    runtime_status = _runtime_status_for_readiness(readiness)
    last_checked_at = (
        _iso(worker_instance.observed_at)
        if (worker_instance is not None and worker_instance.observed_at is not None)
        else (_text(observation.get("checked_at")) if observation is not None else None)
    )
    return {
        "environment_id": environment.id,
        "employee_id": environment.employee_id,
        "enabled": bool(employee_enabled),
        "readiness": readiness,
        "target_digest": want_digest,
        "target_matched": target_matched,
        "connection": {
            "channel": "worker",
            "state": "online" if online else "offline",
            "last_heartbeat_at": _iso(worker_instance.last_heartbeat_at)
            if worker_instance is not None
            else None,
            "observed_at": _iso(worker_instance.observed_at)
            if worker_instance is not None
            else None,
            "epoch": worker_instance.epoch if worker_instance is not None else None,
        },
        "maintenance": _maintenance_summary(latest_operation),
        "runtime_health": {
            "status": runtime_status,
            "readiness": readiness,
            "last_checked_at": last_checked_at,
            "last_error": opencode_last_error,
            "equipment": rows,
        },
        "repository": repo_summary,
        "schedulable": len(blockers) == 0,
        "blocking": blockers,
    }


def _evaluate_local_readiness(
    *,
    desired: dict[str, Any],
    environment: OpenCodeExecutionEnvironment,
    want_digest: str,
    employee_enabled: bool,
    occupying_task_ids: Sequence[str],
    active_session_ids: Sequence[str],
    latest_operation: WorkerMaintenanceOperation | None,
    base_blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    blockers = list(base_blockers)
    detail = environment.status_detail if isinstance(environment.status_detail, dict) else {}
    local_obs = detail.get("local_observation") if isinstance(detail.get("local_observation"), dict) else None
    observation = _normalize_snapshot(local_obs, None, default_source="local") if local_obs else None
    rows = build_equipment_rows(desired, observation)
    repo_summary = build_repository_summary(desired, observation)

    if not _text(environment.repository_id):
        readiness = WorkerReadiness.DRIFTED.value
        blockers.append({"code": "repository_missing", "message": "尚未绑定 Git 仓库"})
    elif environment.status == EnvironmentStatus.UNAVAILABLE:
        readiness = WorkerReadiness.DEGRADED.value
        message = _text(detail.get("message")) or "本地运行环境不可用"
        blockers.append({"code": "runtime_degraded", "message": redact_text(message)})
    elif environment.status != EnvironmentStatus.AVAILABLE:
        readiness = WorkerReadiness.UNKNOWN.value
        blockers.append(
            {
                "code": f"runtime_{str(environment.status).lower()}",
                "message": "本地运行环境尚未通过检查",
            }
        )
    elif observation is not None:
        row_states = [str(row.get("state") or "unknown") for row in rows if row.get("configured")]
        if any(state == "degraded" for state in row_states):
            readiness = WorkerReadiness.DEGRADED.value
            blockers.append({"code": "runtime_degraded", "message": "本地运行自检异常"})
        elif any(state == "drifted" for state in row_states):
            readiness = WorkerReadiness.DRIFTED.value
            blockers.append({"code": "config_drifted", "message": "本地配置已变更，待同步"})
        else:
            readiness = WorkerReadiness.READY.value
    else:
        readiness = WorkerReadiness.READY.value

    _append_occupancy_blockers(blockers, occupying_task_ids, active_session_ids)
    runtime_status = _runtime_status_for_readiness(readiness)
    return {
        "environment_id": environment.id,
        "employee_id": environment.employee_id,
        "enabled": bool(employee_enabled),
        "readiness": readiness,
        "target_digest": want_digest,
        "target_matched": True,
        "connection": {
            "channel": "local",
            "state": "local",
            "last_heartbeat_at": None,
            "observed_at": _iso(environment.last_checked_at),
            "epoch": None,
        },
        "maintenance": _maintenance_summary(latest_operation),
        "runtime_health": {
            "status": runtime_status,
            "readiness": readiness,
            "last_checked_at": _iso(environment.last_checked_at),
            "last_error": _extract_opencode_last_error(observation),
            "equipment": rows,
        },
        "repository": repo_summary,
        "schedulable": len(blockers) == 0,
        "blocking": blockers,
    }


def build_equipment_rows(
    desired: dict[str, Any] | None,
    observation: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    want = desired if isinstance(desired, dict) else {}
    seen = observation if isinstance(observation, dict) else {}
    return [
        _opencode_row(want, seen),
        _worker_agent_row(want, seen),
        _model_row(want, seen),
        _mcp_row(want, seen),
        _skills_row(want, seen),
        _git_row(want, seen),
    ]


def build_repository_summary(
    desired: dict[str, Any] | None,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    want = desired if isinstance(desired, dict) else {}
    seen = observation if isinstance(observation, dict) else {}
    repository = want.get("repository") if isinstance(want.get("repository"), dict) else {}
    observed = seen.get("repository") if isinstance(seen.get("repository"), dict) else {}
    desired_url = sanitize_url(repository.get("url"))
    actual_url = sanitize_url(observed.get("url"))
    status = _status(observed)
    error = None
    if not repository:
        error = "尚未绑定 Git 仓库"
    elif _text(repository.get("error")):
        error = _text(repository.get("error"))
    elif not observed:
        error = "Git 仓库待部署核验"
    elif status == UNKNOWN:
        error = _text(observed.get("error")) or "Git 仓库未核验"
    elif status == HEALTHY:
        if desired_url and actual_url and desired_url != actual_url:
            error = "实际仓库地址与期望不一致"
        elif _text(repository.get("target_branch")) and (
            _text(observed.get("branch")) != _text(repository.get("target_branch"))
        ):
            error = "实际分支与期望分支不一致"
        elif not _text(observed.get("commit")):
            error = "未检测到有效 HEAD"
    else:
        error = _text(observed.get("error")) or "Git 仓库探测失败"
    return {
        "id": repository.get("id"),
        "url": actual_url or desired_url or None,
        "target_branch": repository.get("target_branch"),
        "branch": _text(observed.get("branch")) or None,
        "commit": _text(observed.get("commit")) or None,
        "dirty": observed.get("dirty") if isinstance(observed.get("dirty"), bool) else None,
        "authentication": repository.get("authentication"),
        "error": redact_text(error) if error else None,
    }


def _opencode_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    want = _text((desired.get("opencode") or {}).get("version"))
    section = _section(observation, "opencode")
    actual = _text(section.get("version"))
    status = _status(section)
    last_error = _text(section.get("last_error"))
    error = _text(section.get("error")) or (last_error.splitlines()[-1] if last_error else "")
    matched = bool(want) and _same_version(want, actual)
    if status == UNHEALTHY:
        state = "degraded"
        verified: bool | None = False
        diff = [f"OpenCode 服务异常：{error}" if error else "OpenCode 健康探测失败"]
    elif status == HEALTHY:
        if matched:
            state = "ready"
            verified = True
            diff = []
        else:
            state = "drifted"
            verified = False
            diff = [f"OpenCode 版本待同步：期望 {want}，当前 {actual or '未知'}"]
    else:
        state = "unknown"
        verified = None
        diff = ["OpenCode 尚未核验"]
    return _row(
        key="opencode",
        label="OpenCode",
        state=state,
        configured=bool(want),
        deployed=matched,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=error or None,
        detail=f"OpenCode {actual}" if actual else None,
    )


def _worker_agent_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    want = _text((desired.get("worker_agent") or {}).get("version"))
    actual = _text(observation.get("worker_agent_version"))
    source = _text(observation.get("source"))
    if source == "local":
        return _row(
            key="worker_agent",
            label="Worker Agent",
            state="ready",
            configured=False,
            deployed=True,
            verified=True,
            desired_revision=None,
            actual_revision="local",
            diff=[],
            error=None,
            detail="本地模式无需远程守护",
        )
    if actual and want:
        matched = _same_version(want, actual)
        state = "ready" if matched else "drifted"
        verified: bool | None = matched
        diff = [] if matched else [f"Worker Agent 版本待同步：期望 {want}，当前 {actual}"]
    else:
        matched = False
        state = "unknown"
        verified = None
        diff = ["Worker Agent 尚未建立在线握手"]
    return _row(
        key="worker_agent",
        label="Worker Agent",
        state=state,
        configured=bool(want),
        deployed=matched,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=None,
        detail=f"Worker Agent {actual}" if actual else None,
    )


def _model_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    model = desired.get("model") if isinstance(desired.get("model"), dict) else {}
    want = _text(model.get("name"))
    resolved = model.get("resolved") is not False
    desired_error = _text(model.get("error"))
    section = _section(observation, "model")
    actual = _text(section.get("model"))
    status = _status(section)
    configured_ok = section.get("configured") is True
    matched = bool(want) and resolved and configured_ok and actual == want
    if not resolved or not want:
        state = "drifted"
        verified: bool | None = False
        diff = [desired_error or "员工模型未匹配启用的中转路由"]
    elif status == UNHEALTHY:
        if actual and actual != want:
            state = "drifted"
            verified = False
            diff = [f"模型待同步：期望 {want}，当前 {actual}"]
        else:
            state = "degraded"
            verified = False
            diff = [_text(section.get("error")) or "模型配置读取失败"]
    elif status == HEALTHY:
        if matched:
            state = "ready"
            verified = True
            diff = []
        else:
            state = "drifted"
            verified = False
            diff = [f"模型待同步：期望 {want}，当前 {actual or '未配置'}"]
    else:
        state = "unknown"
        verified = None
        diff = ["模型配置尚未核验"]
    return _row(
        key="model",
        label="模型",
        state=state,
        configured=True,
        deployed=bool(want) and actual == want,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=_text(section.get("error")) or desired_error or None,
        detail=f"生效模型 {actual}" if actual else None,
    )


def _mcp_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    mcp = desired.get("mcp") if isinstance(desired.get("mcp"), dict) else {}
    items = [item for item in mcp.get("items") or [] if isinstance(item, dict)]
    want_rev = _text(mcp.get("revision"))
    required = [str(name) for name in mcp.get("required") or [] if name]
    section = _section(observation, "mcp")
    status = _status(section)
    missing = [str(name) for name in section.get("missing") or [] if name]
    endpoints = section.get("endpoints") if isinstance(section.get("endpoints"), dict) else None
    drift_problems: list[str] = []
    if endpoints is not None:
        for item in items:
            name = _text(item.get("name"))
            want_url = sanitize_url(item.get("url"))
            if not name or not want_url:
                continue
            if name not in endpoints:
                drift_problems.append(f"{name} 尚未部署")
            elif sanitize_url(endpoints.get(name)) != want_url:
                drift_problems.append(f"{name} 地址待同步")
    actual_rev = (
        mcp_endpoints_revision(
            [
                {"name": _text(item.get("name")), "url": endpoints.get(_text(item.get("name")))}
                for item in items
                if _text(item.get("name")) and _text(item.get("name")) in endpoints
            ]
        )
        if endpoints is not None
        else ""
    )
    deployed = bool(want_rev) and actual_rev == want_rev and not drift_problems
    if drift_problems:
        state = "drifted"
        verified: bool | None = False
        diff = [f"MCP 待同步：{'、'.join(drift_problems)}"]
    elif status == UNHEALTHY or missing:
        state = "degraded"
        verified = False
        diff = (
            ["MCP 连接异常：" + "、".join(missing)]
            if missing
            else [_text(section.get("error")) or "MCP 状态探测失败"]
        )
    elif status == HEALTHY:
        if endpoints is None and items:
            state = "unknown"
            verified = None
            diff = ["实际 MCP 端点未知"]
        else:
            state = "ready"
            verified = True
            diff = []
    else:
        state = "unknown"
        verified = None
        diff = ["MCP 尚未核验"]
    return _row(
        key="mcp",
        label="MCP",
        state=state,
        configured=bool(required),
        deployed=deployed,
        verified=verified,
        desired_revision=want_rev or None,
        actual_revision=actual_rev or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=(
            "已连接：" + "、".join(required)
            if state == "ready" and required
            else None
        ),
    )


def _skills_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    skills = desired.get("skills") if isinstance(desired.get("skills"), dict) else {}
    items = [item for item in skills.get("items") or [] if isinstance(item, dict)]
    want_content = _text(skills.get("content_revision")) or _text(skills.get("revision"))
    source_missing = [str(name) for name in skills.get("missing_sources") or [] if name]
    unverifiable = [str(name) for name in skills.get("unverifiable_sources") or [] if name]
    section = _section(observation, "skills")
    status = _status(section)
    content_hashes = (
        section.get("content_hashes") if isinstance(section.get("content_hashes"), dict) else None
    )

    if not items:
        state = "ready" if status == HEALTHY else ("degraded" if status == UNHEALTHY else "unknown")
        verified: bool | None = True if status == HEALTHY else (False if status == UNHEALTHY else None)
        return _row(
            key="skills",
            label="Skill",
            state=state,
            configured=False,
            deployed=True,
            verified=verified,
            desired_revision=None,
            actual_revision=None,
            diff=[],
            error=_text(section.get("error")) or None,
            detail="未绑定额外 Skill",
        )

    drift_problems: list[str] = []
    if source_missing:
        drift_problems.append("平台源缺失：" + "、".join(source_missing))
    if unverifiable:
        drift_problems.append("内容不可核验：" + "、".join(unverifiable))
    if content_hashes is not None:
        for item in items:
            name = _text(item.get("name"))
            want_hash = _text(item.get("content_hash"))
            if not name or not want_hash:
                continue
            if name not in content_hashes:
                drift_problems.append(f"{name} 尚未部署")
            elif _text(content_hashes.get(name)) != want_hash:
                drift_problems.append(f"{name} 内容待同步")

    managed_names = sorted({_text(item.get("name")) for item in items if _text(item.get("name"))})
    actual_content_revision = ""
    if content_hashes is not None and managed_names:
        observed_items = [
            {"name": name, "content_hash": _text(content_hashes.get(name))}
            for name in managed_names
            if _text(content_hashes.get(name))
        ]
        if len(observed_items) == len(managed_names):
            actual_content_revision = _skills_content_revision(observed_items)

    deployed = bool(want_content) and actual_content_revision == want_content and not drift_problems
    if drift_problems:
        state = "drifted"
        verified = False
        diff = [f"Skill 待同步：{'；'.join(drift_problems)}"]
    elif status == UNHEALTHY:
        # 若只是未发现尚未部署的期望 Skill，归类为待同步；若 /skill 端点自身报错，归类为降级。
        missing = [str(name) for name in section.get("missing") or [] if name]
        if missing and not _text(section.get("error")):
            state = "drifted"
            verified = False
            diff = ["Skill 待同步：" + "、".join(f"{name} 尚未部署" for name in missing)]
        else:
            state = "degraded"
            verified = False
            diff = [_text(section.get("error")) or "Skill 目录探测失败"]
    elif status == HEALTHY:
        if content_hashes is None:
            state = "unknown"
            verified = None
            diff = ["实际 Skill 内容摘要缺失"]
        else:
            state = "ready"
            verified = True
            diff = []
    else:
        state = "unknown"
        verified = None
        diff = ["Skill 尚未核验"]
    return _row(
        key="skills",
        label="Skill",
        state=state,
        configured=True,
        deployed=deployed,
        verified=verified,
        desired_revision=want_content or None,
        actual_revision=actual_content_revision or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=(
            "已生效：" + "、".join(managed_names)
            if state == "ready" and managed_names
            else None
        ),
    )


def _git_row(desired: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    repository = desired.get("repository") if isinstance(desired.get("repository"), dict) else {}
    section = _section(observation, "repository")
    status = _status(section)
    if not repository:
        return _row(
            key="git",
            label="Git 仓库",
            state="drifted",
            configured=False,
            deployed=False,
            verified=None,
            desired_revision=None,
            actual_revision=None,
            diff=["未绑定 Git 仓库，节点不可调度"],
            error="未绑定 Git 仓库",
            detail=None,
        )

    want_rev = _repository_revision(repository)
    desired_error = _text(repository.get("error"))
    expected_url = sanitize_url(repository.get("url"))
    actual_url = sanitize_url(section.get("url"))
    expected_branch = _text(repository.get("target_branch"))
    actual_branch = _text(section.get("branch"))
    commit = _text(section.get("commit"))
    want_cred_rev = _text(repository.get("credential_revision"))
    actual_cred_rev = _text(section.get("credential_revision"))

    if desired_error:
        return _row(
            key="git",
            label="Git 仓库",
            state="drifted",
            configured=True,
            deployed=False,
            verified=False,
            desired_revision=want_rev or None,
            actual_revision=commit or None,
            diff=[desired_error],
            error=desired_error,
            detail=None,
        )

    drift_reasons: list[str] = []
    if status in {HEALTHY, UNHEALTHY} and actual_url and expected_url and actual_url != expected_url:
        drift_reasons.append("仓库地址已变更，待重新部署")
    if status == HEALTHY and expected_branch and actual_branch and actual_branch != expected_branch:
        drift_reasons.append(f"分支待同步：期望 {expected_branch}，当前 {actual_branch}")
    if (
        status == HEALTHY
        and want_cred_rev
        and actual_cred_rev
        and actual_cred_rev != want_cred_rev
    ):
        drift_reasons.append("仓库访问凭据已更新，待重新部署同步")

    if drift_reasons:
        state = "drifted"
        verified: bool | None = False
        deployed = False
        diff = list(drift_reasons)
    elif status == UNHEALTHY:
        state = "degraded"
        verified = False
        deployed = False
        diff = [_text(section.get("error")) or "Git 工作区异常"]
    elif status == HEALTHY:
        if not commit or (expected_url and actual_url != expected_url) or (
            expected_branch and actual_branch != expected_branch
        ):
            state = "drifted"
            verified = False
            deployed = False
            diff = ["Git 工作区与期望仓库/分支不一致"]
        elif section.get("authentication_verified") is False:
            state = "degraded"
            verified = False
            deployed = True
            diff = [_text(section.get("error")) or "远端仓库认证校验失败"]
        else:
            state = "ready"
            verified = True
            deployed = True
            diff = []
            if section.get("dirty") is True:
                diff.append("工作区存在未提交变更（仅阻止代码更新，不影响任务运行）")
    else:
        state = "unknown"
        verified = None
        deployed = False
        diff = ["Git 仓库尚未核验"]

    return _row(
        key="git",
        label="Git 仓库",
        state=state,
        configured=True,
        deployed=deployed,
        verified=verified,
        desired_revision=want_rev or None,
        actual_revision=commit or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=f"{actual_branch}@{commit[:12]}" if actual_branch and commit else None,
    )


def _desired_model(
    environment: OpenCodeExecutionEnvironment, context: ReadinessLookupContext
) -> dict[str, Any]:
    configured = _text(environment.model_name)
    route = (
        route_model_id(configured)
        if configured
        else (context.ordered_chat_routes[0] if context.ordered_chat_routes else "")
    )
    resolved = bool(route) and route in context.enabled_chat_routes
    error = None
    if not route:
        error = "没有启用的 GatewayRoute"
    elif not resolved:
        error = "员工模型未匹配启用的中转路由"
    return {
        "name": f"{GATEWAY_PROVIDER}/{route}" if route else "",
        "route": route or None,
        "provider": GATEWAY_PROVIDER,
        "source": "gateway_route",
        "resolved": resolved,
        "error": error,
    }


def _desired_repository(
    environment: OpenCodeExecutionEnvironment, context: ReadinessLookupContext
) -> dict[str, Any] | None:
    repository_id = _text(environment.repository_id)
    if not repository_id:
        return None
    repository = context.repositories.get(repository_id)
    if repository is None:
        return {
            "id": repository_id,
            "url": None,
            "target_branch": _text(environment.branch),
            "authentication": "missing",
            "credential_revision": None,
            "error": "绑定的仓库不存在",
        }
    ciphertext = context.credential_ciphertext_by_repo.get(repository_id, "")
    return {
        "id": repository.id,
        "url": sanitize_url(repository.url),
        "target_branch": _text(environment.branch) or _text(repository.branch) or "main",
        "authentication": "token" if ciphertext else "none",
        "credential_revision": _hash(ciphertext) if ciphertext else None,
        "error": None,
    }


def _desired_skills(
    environment: OpenCodeExecutionEnvironment, context: ReadinessLookupContext
) -> dict[str, Any]:
    names = sorted(
        {str(name).strip() for name in (environment.skill_refs or []) if str(name).strip()}
    )
    items = []
    for name in names:
        body = context.skill_bodies.get(name)
        items.append(
            {
                "name": name,
                "content_hash": skill_content_hash(body) if body is not None else None,
                "fullfile_hash": _hash(body) if body is not None else None,
            }
        )
    missing = [name for name in names if name not in context.skill_bodies]
    unverifiable = [
        item["name"]
        for item in items
        if item["name"] in context.skill_bodies and item["content_hash"] is None
    ]
    return {
        "items": items,
        "missing_sources": missing,
        "unverifiable_sources": unverifiable,
        "revision": _skills_revision(items),
        "content_revision": _skills_content_revision(items),
    }


def _desired_mcp(
    environment: OpenCodeExecutionEnvironment, settings: Settings
) -> dict[str, Any]:
    default_url = sanitize_url(getattr(settings, "mcp_resource_server_url", ""))
    urls: dict[str, str] = {DEFAULT_MCP: default_url}
    for item in environment.mcp_refs or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        url = sanitize_url(item.get("url"))
        if name and url:
            urls[name] = url
    items = [
        {"name": name, "url": urls.get(name) or "", "required": True}
        for name in sorted(urls)
    ]
    return {
        "items": items,
        "required": [item["name"] for item in items],
        "revision": mcp_endpoints_revision(items),
    }


def _normalize_snapshot(
    snapshot: dict[str, Any] | None,
    worker_instance: WorkerInstance | None,
    *,
    default_source: str = "worker",
) -> dict[str, Any]:
    raw = snapshot if isinstance(snapshot, dict) else {}
    agent_ver = (
        _text(worker_instance.agent_version)
        if (worker_instance is not None and _text(worker_instance.agent_version))
        else _text(raw.get("worker_agent_version"))
    )
    normalized: dict[str, Any] = {
        "spec_revision": raw.get("spec_revision"),
        "opencode": raw.get("opencode") if isinstance(raw.get("opencode"), dict) else {},
        "model": raw.get("model") if isinstance(raw.get("model"), dict) else {},
        "mcp": raw.get("mcp") if isinstance(raw.get("mcp"), dict) else {},
        "skills": raw.get("skills") if isinstance(raw.get("skills"), dict) else {},
        "repository": raw.get("repository") if isinstance(raw.get("repository"), dict) else {},
        "checked_at": _text(raw.get("checked_at")) or None,
        "worker_agent_version": agent_ver or None,
        "source": _text(raw.get("source")) or default_source,
        "error": _text(raw.get("error")) or None,
    }
    return redact_data(normalized)


def _extract_opencode_last_error(observation: dict[str, Any] | None) -> str | None:
    if not isinstance(observation, dict):
        return None
    opencode = observation.get("opencode")
    if not isinstance(opencode, dict):
        return None
    tail = _text(opencode.get("last_error"))
    return redact_text(tail) if tail else None


def _append_occupancy_blockers(
    blockers: list[dict[str, Any]],
    occupying_task_ids: Sequence[str],
    active_session_ids: Sequence[str],
) -> None:
    for task_id in occupying_task_ids:
        blockers.append(
            {
                "code": "occupied_task",
                "message": "员工存在运行中或等待介入的任务",
                "task_id": str(task_id),
            }
        )
    for session_id in active_session_ids:
        blockers.append(
            {
                "code": "active_session",
                "message": "员工存在未结束的运行会话",
                "session_id": str(session_id),
            }
        )


def _maintenance_summary(operation: WorkerMaintenanceOperation | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    return {
        "state": operation.state,
        "action": operation.action,
        "operation_id": operation.id,
        "stage": operation.stage,
        "attempt": operation.attempt,
        "error_message": operation.error_message,
    }


def _runtime_status_for_readiness(readiness: str) -> str:
    if readiness == WorkerReadiness.READY.value:
        return EnvironmentStatus.AVAILABLE.value
    if readiness == WorkerReadiness.DRIFTED.value:
        return EnvironmentStatus.DRIFTED.value
    if readiness == WorkerReadiness.DEGRADED.value:
        return EnvironmentStatus.UNAVAILABLE.value
    return EnvironmentStatus.UNKNOWN.value


def _row(
    *,
    key: str,
    label: str,
    state: str,
    configured: bool,
    deployed: bool,
    verified: bool | None,
    desired_revision: str | None,
    actual_revision: str | None,
    diff: list[str],
    error: str | None,
    detail: str | None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "state": state,
        "configured": bool(configured),
        "deployed": bool(deployed),
        "verified": verified,
        "desired_revision": desired_revision,
        "actual_revision": actual_revision,
        "diff": [redact_text(item) for item in diff if item],
        "error": redact_text(error) if error else None,
        "detail": redact_text(detail) if detail else None,
    }


def _section(observation: dict[str, Any], key: str) -> dict[str, Any]:
    section = observation.get(key)
    return section if isinstance(section, dict) else {}


def _status(section: dict[str, Any]) -> str:
    return _text(section.get("status")) or UNKNOWN


def _same_version(want: str, actual: str) -> bool:
    return _text(want).lower().removeprefix("v") == _text(actual).lower().removeprefix("v")


def _skills_revision(items: list[dict[str, Any]]) -> str:
    rows = [
        {"name": item.get("name"), "fullfile_hash": item.get("fullfile_hash")}
        for item in items
        if item.get("name") and item.get("fullfile_hash")
    ]
    return _revision(rows) if rows else ""


def _skills_content_revision(items: list[dict[str, Any]]) -> str:
    rows = [
        {"name": item.get("name"), "content_hash": item.get("content_hash")}
        for item in items
        if item.get("name") and item.get("content_hash")
    ]
    return _revision(rows) if rows else ""


def _repository_revision(repository: dict[str, Any]) -> str:
    if not repository:
        return ""
    return _revision(
        {
            "url": sanitize_url(repository.get("url")),
            "target_branch": repository.get("target_branch"),
            "authentication": repository.get("authentication"),
            "credential_revision": repository.get("credential_revision"),
        }
    )


def sanitize_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        return re.sub(r"^[^/@\s]+@", "", text.split("?", 1)[0].split("#", 1)[0])
    try:
        parts = urlsplit(text)
        if parts.scheme == "file":
            return urlunsplit(("file", "", parts.path, "", ""))
        host = parts.hostname or ""
        if not host:
            return ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = f":{parts.port}" if parts.port else ""
        except ValueError:
            port = ""
        return urlunsplit((parts.scheme, host + port, parts.path, "", ""))
    except ValueError:
        return ""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _revision(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
