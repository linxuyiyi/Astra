"""Worker 装备期望、真实只读探测与 canonical 健康投影。

本模块只做三件事：

1. ``desired_equipment``：从当前数据库读取期望装备，不把
   ``status_detail.desired`` 当成事实源。
2. ``probe_equipment``：优先通过在线 Worker 的只读命令观察，离线时通过 SSH
   使用每环境 venv 导入 ``worker_agent.inspection``；不安装、不重启、不改配置。
3. ``equipment_rows`` / ``repository_payload``：生成 UI 只认的固定结构。

期望摘要（desired）结构::

    {
      "opencode": {"version": "1.18.35"},
      "worker_agent": {"version": "1.4.5"},
      "model": {"name", "route", "provider", "source", "resolved", "error"},
      "repository": null | {
          "id", "url", "target_branch", "authentication",
          "credential_revision", "error"
      },
      "skills": {"items": [{"name", "content_hash", "fullfile_hash"}],
                 "missing_sources": [...], "unverifiable_sources": [...],
                 "revision": "部署 source revision",
                 "content_revision": "语义 body revision"},
      "mcp": {"items": [{"name", "url", "required"}],
              "required": [...], "revision"},
      "revision": "摘要 revision"
    }

观察（observation）包含 inspection 的 ``opencode/model/mcp/skills/repository``
各节，以及 ``worker_agent_version``、``checked_at``、``observed_epoch``。
``mcp.endpoints`` 来自生效 ``/config``；``skills.content_hashes`` 来自
``/skill`` 的语义 body。配置或 content 不可读时对应字段为 ``None``。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.database import SessionLocal
from app.domain import EnvironmentStatus
from app.gateway.models import GatewayRoute
from app.models import (
    CodeRepository,
    GitCredential,
    OpenCodeExecutionEnvironment,
    WorkerInstance,
)
from app.runtime.capability import binding_entries, binding_revision
from app.runtime.environment import ExecutionEnvironmentRuntime
from app.runtime.opencode_model import route_model_id
from app.runtime.redaction import redact_data, redact_text
from app.runtime.repository import collect_skill_files
from app.runtime.worker_client import WorkerOpenCodeClient
from app.worker_agent import __version__ as WORKER_AGENT_VERSION
from app.worker_agent.inspection import skill_content_hash

DEFAULT_MCP = "nexus-library"
GATEWAY_PROVIDER = "nexus-gateway"
REQUIRED_SECTIONS = ("opencode", "model", "mcp", "skills", "repository")
HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
UNKNOWN = "unknown"
_ENV_ALLOW = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "NEXUS_WORKER_OPENCODE_URL",
    "NEXUS_WORKER_WORKSPACE_ROOT",
    "NEXUS_WORKER_OPENCODE_UNIT",
    "GIT_ASKPASS",
    "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
)


async def desired_equipment(
    session: AsyncSession,
    environment: OpenCodeExecutionEnvironment,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """当前数据库配置的期望装备摘要。只返回非 secret 值。"""

    resolved = settings or get_settings()
    payload: dict[str, Any] = {
        "opencode": {"version": str(resolved.remote_opencode_version)},
        "worker_agent": {"version": WORKER_AGENT_VERSION},
        "model": await _desired_model(session, environment),
        "repository": await _desired_repository(session, environment),
        "skills": _desired_skills(environment, resolved),
        "mcp": _desired_mcp(environment, resolved),
    }
    if environment.runtime_engine == "dsh":
        from app.worker_agent.dsh_acp.profile import ACP_VERSION, DSH_VERSION
        payload.pop("opencode")
        payload["runtime_engine"] = "dsh"
        payload["dsh"] = {"version": DSH_VERSION, "acp": ACP_VERSION}
        payload["model"]["name"] = payload["model"].get("route") or ""
        payload["mcp"] = {"items": list(environment.mcp_refs or [])}
    payload["revision"] = _revision(payload)
    return payload


async def probe_equipment(
    environment: OpenCodeExecutionEnvironment,
    desired: dict[str, Any],
    settings: Settings | None = None,
    *,
    worker_instance: WorkerInstance | None = None,
    observed_epoch: int | None = None,
    perenv_python: str | None = None,
) -> dict[str, Any]:
    """只读观察当前装备。在线 Worker 优先，离线时才使用 SSH。"""

    resolved = settings or get_settings()
    instance = worker_instance if worker_instance is not None else await _worker_instance(environment)
    if environment.runtime_engine == "dsh":
        from app.runtime.worker_transport import WorkerCommandTransport
        try:
            result = await WorkerCommandTransport(environment_id=environment.id).run_command("dsh_health", {})
            return {"runtime_engine": "dsh", "dsh": result, "repository": result.get("repository", {}),
                    "source": "worker", "worker_agent_version": WORKER_AGENT_VERSION,
                    "error": result.get("error")}
        except Exception:
            return {"runtime_engine": "dsh", "error": "DSH Worker 固定装配或 Gateway 检查失败"}
    expected = _expected_payload(desired)
    if instance is not None and instance.status == "online":
        try:
            client = WorkerOpenCodeClient(resolved, environment_id=environment.id)
            result = await client.run_command("inspect_runtime", expected)
        except Exception as exc:  # noqa: BLE001 - 探测失败必须变成 observation
            return _failed_observation(exc, instance, observed_epoch, source="worker")
        return _observation(result, instance, observed_epoch, source="worker")

    if _ssh_available(environment):
        try:
            result = await _probe_via_ssh(environment, expected, resolved, perenv_python)
        except Exception as exc:  # noqa: BLE001 - SSH 失败必须变成 observation
            return _failed_observation(exc, instance, observed_epoch, source="ssh")
        if result.get("failure"):
            return _failed_observation(
                result["failure"],
                instance,
                observed_epoch,
                source="ssh",
                not_deployed=bool(result.get("not_deployed")),
            )
        return _observation(result, instance, observed_epoch, source="ssh")

    return _failed_observation(
        "Worker 不在线且未配置 SSH，无法探测",
        instance,
        observed_epoch,
        source="none",
    )


def health_status(
    observation: dict[str, Any],
    desired: dict[str, Any] | None = None,
) -> EnvironmentStatus:
    """纯函数：只有全部必需环节真实 healthy 才是 AVAILABLE。

    Worker Gateway 连接状态由 API 单独返回，不混入这里。
    """

    if not isinstance(observation, dict) or observation.get("error"):
        return EnvironmentStatus.UNAVAILABLE
    if not isinstance(desired, dict):
        return EnvironmentStatus.UNAVAILABLE
    if desired.get("runtime_engine") == "dsh":
        dsh = observation.get("dsh", {})
        expected = {str(x.get("name")) for x in (desired.get("mcp") or {}).get("items", [])}
        ready = (dsh.get("healthy") is True and dsh.get("protocol_initialized") is True
                 and dsh.get("file_channel_verified") is True
                 and (desired.get("model") or {}).get("route") in dsh.get("models", [])
                 and all(dsh.get("skill_hashes", {}).get(x["name"], {}).get("SKILL.md") == x["fullfile_hash"]
                    for x in (desired.get("skills") or {}).get("items", []))
                 and expected == set(dsh.get("mcp_names", []))
                 and _matches_repository(desired, observation))
        return EnvironmentStatus.AVAILABLE if ready else EnvironmentStatus.UNAVAILABLE
    for key in REQUIRED_SECTIONS:
        section = observation.get(key)
        if not isinstance(section, dict) or section.get("status") != HEALTHY:
            return EnvironmentStatus.UNAVAILABLE
    if not _matches_opencode(desired, observation):
        return EnvironmentStatus.UNAVAILABLE
    if not _matches_model(desired, observation):
        return EnvironmentStatus.UNAVAILABLE
    if not _matches_mcp(desired, observation):
        return EnvironmentStatus.UNAVAILABLE
    if not _matches_skills(desired, observation):
        return EnvironmentStatus.UNAVAILABLE
    if not _matches_repository(desired, observation):
        return EnvironmentStatus.UNAVAILABLE
    if observation.get("source") == "worker":
        expected_agent = _text((desired.get("worker_agent") or {}).get("version"))
        actual_agent = _text(observation.get("worker_agent_version"))
        if expected_agent and not _same_version(expected_agent, actual_agent):
            return EnvironmentStatus.UNAVAILABLE
    return EnvironmentStatus.AVAILABLE


def _matches_opencode(desired: dict[str, Any], observation: dict[str, Any]) -> bool:
    want = _text((desired.get("opencode") or {}).get("version"))
    actual = _text(_section(observation, "opencode").get("version"))
    return bool(want) and _same_version(want, actual)


def _matches_model(desired: dict[str, Any], observation: dict[str, Any]) -> bool:
    want = _text((desired.get("model") or {}).get("name"))
    section = _section(observation, "model")
    return bool(want) and _text(section.get("model")) == want and section.get("authenticated") is True


def _matches_mcp(desired: dict[str, Any], observation: dict[str, Any]) -> bool:
    mcp = desired.get("mcp") if isinstance(desired.get("mcp"), dict) else {}
    items = [item for item in mcp.get("items") or [] if isinstance(item, dict)]
    endpoints = _section(observation, "mcp").get("endpoints")
    for item in items:
        name = _text(item.get("name"))
        url = _sanitize_url(item.get("url"))
        if not name or not url:
            continue
        if not isinstance(endpoints, dict) or name not in endpoints:
            return False
        if _sanitize_url(endpoints.get(name)) != url:
            return False
    return True


def _matches_skills(desired: dict[str, Any], observation: dict[str, Any]) -> bool:
    skills = desired.get("skills") if isinstance(desired.get("skills"), dict) else {}
    items = [item for item in skills.get("items") or [] if isinstance(item, dict)]
    content_hashes = _section(observation, "skills").get("content_hashes")
    for item in items:
        name = _text(item.get("name"))
        want_hash = _text(item.get("content_hash"))
        if not name:
            continue
        if not want_hash or not isinstance(content_hashes, dict):
            return False
        if content_hashes.get(name) != want_hash:
            return False
    return True


def _matches_repository(desired: dict[str, Any], observation: dict[str, Any]) -> bool:
    repository = desired.get("repository")
    if not isinstance(repository, dict):
        return False
    section = _section(observation, "repository")
    want_url = _sanitize_url(repository.get("url"))
    actual_url = _sanitize_url(section.get("url"))
    want_branch = _text(repository.get("target_branch"))
    actual_branch = _text(section.get("branch"))
    commit = _text(section.get("commit"))
    if (
        repository.get("authentication") == "token"
        and section.get("authentication_verified") is not True
    ):
        return False
    return bool(
        want_url
        and want_branch
        and commit
        and actual_url == want_url
        and actual_branch == want_branch
    )


def equipment_rows(
    desired: dict[str, Any] | None,
    observation: dict[str, Any] | None,
    deployed_snapshot: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """固定六项装备投影。deployed 只来自部署快照，verified 只来自真实探测。"""

    want = desired if isinstance(desired, dict) else {}
    seen = observation if isinstance(observation, dict) else {}
    snapshot = deployed_snapshot if isinstance(deployed_snapshot, dict) else {}
    if want.get("runtime_engine") == "dsh":
        dsh = seen.get("dsh", {})
        ready = health_status(seen, want) == EnvironmentStatus.AVAILABLE
        return [{"key": "dsh", "label": "DSH ACP", "configured": True,
                 "deployed": bool(dsh.get("dsh")), "verified": ready,
                 "desired_revision": (want.get("dsh") or {}).get("version"),
                 "actual_revision": dsh.get("dsh"), "error": seen.get("error"),
                 "detail": "固定装配、文件通道、Gateway 模型目录和仓库核对"}, _git_row(want, seen, snapshot)]
    return [
        _opencode_row(want, seen, snapshot),
        _worker_agent_row(want, seen, snapshot),
        _model_row(want, seen, snapshot),
        _mcp_row(want, seen, snapshot),
        _skills_row(want, seen, snapshot),
        _git_row(want, seen, snapshot),
    ]


def repository_payload(
    desired: dict[str, Any] | None,
    observation: dict[str, Any] | None,
) -> dict[str, Any]:
    """UI 只认的仓库结构。URL 去掉 userinfo/query/fragment，错误已脱敏。"""

    want = desired if isinstance(desired, dict) else {}
    seen = observation if isinstance(observation, dict) else {}
    repository = want.get("repository") if isinstance(want.get("repository"), dict) else {}
    observed = seen.get("repository") if isinstance(seen.get("repository"), dict) else {}
    desired_url = _sanitize_url(repository.get("url"))
    actual_url = _sanitize_url(observed.get("url"))
    status = _status(observed)
    error = None
    if not observed:
        if repository:
            error = "Git 仓库未核验"
    elif status == UNKNOWN:
        error = _redact_text(observed.get("error")) or "Git 仓库未核验"
    elif status == HEALTHY:
        if desired_url and actual_url and desired_url != actual_url:
            error = "实际仓库地址与期望不一致"
        elif repository and observed.get("branch") != repository.get("target_branch"):
            error = "实际分支与期望分支不一致"
        elif not observed.get("commit"):
            error = "未检测到有效 HEAD"
    else:
        error = _redact_text(observed.get("error")) or "Git 仓库探测失败"
    return {
        "id": repository.get("id"),
        "url": actual_url or desired_url or None,
        "target_branch": repository.get("target_branch"),
        "branch": _text(observed.get("branch")) or None,
        "commit": _text(observed.get("commit")) or None,
        "dirty": observed.get("dirty") if isinstance(observed.get("dirty"), bool) else None,
        "authentication": repository.get("authentication"),
        "error": error,
    }


async def _desired_model(
    session: AsyncSession, environment: OpenCodeExecutionEnvironment
) -> dict[str, Any]:
    enabled = list(
        await session.scalars(
            select(GatewayRoute.model)
            .where(GatewayRoute.enabled.is_(True))
            .order_by(GatewayRoute.model)
        )
    )
    configured = _text(environment.model_name)
    route = (configured if environment.runtime_engine == "dsh" else
             route_model_id(configured) if configured else (str(enabled[0]) if enabled else ""))
    resolved = bool(route) and route in enabled
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


async def _desired_repository(
    session: AsyncSession, environment: OpenCodeExecutionEnvironment
) -> dict[str, Any] | None:
    repository_id = _text(environment.repository_id)
    if not repository_id:
        return None
    repository = await session.get(CodeRepository, repository_id)
    if repository is None:
        return {
            "id": repository_id,
            "url": None,
            "target_branch": _text(environment.branch),
            "authentication": "missing",
            "credential_revision": None,
            "error": "绑定的仓库不存在",
        }
    credential = await session.scalar(
        select(GitCredential).where(GitCredential.repository_id == repository_id)
    )
    ciphertext = str(credential.token_ciphertext) if credential is not None else ""
    return {
        "id": repository.id,
        "url": _sanitize_url(repository.url),
        "target_branch": _text(environment.branch) or _text(repository.branch) or "main",
        "authentication": "token" if ciphertext else "none",
        "credential_revision": _hash(ciphertext) if ciphertext else None,
        "error": None,
    }


def _desired_skills(
    environment: OpenCodeExecutionEnvironment, settings: Settings
) -> dict[str, Any]:
    names = sorted(
        {str(name).strip() for name in (environment.skill_refs or []) if str(name).strip()}
    )
    source = Path(settings.workspace_root) / "worker-skills"
    bodies = {item["name"]: item["body"] for item in collect_skill_files(names, source)}
    items = []
    for name in names:
        body = bodies.get(name)
        items.append(
            {
                "name": name,
                "content_hash": skill_content_hash(body) if body is not None else None,
                "fullfile_hash": _hash(body) if body is not None else None,
            }
        )
    missing = [name for name in names if name not in bodies]
    unverifiable = [
        item["name"] for item in items if item["name"] in bodies and item["content_hash"] is None
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
    default_url = _sanitize_url(getattr(settings, "mcp_resource_server_url", ""))
    urls: dict[str, str] = {DEFAULT_MCP: default_url}
    for item in environment.mcp_refs or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name:
            continue
        url = _sanitize_url(item.get("url"))
        if not url:
            continue
        urls[name] = url
    items = [
        {"name": name, "url": urls.get(name) or "", "required": True}
        for name in sorted(urls)
    ]
    entries = binding_entries(None, [{"name": item["name"], "url": item["url"]} for item in items])
    return {
        "items": items,
        "required": [item["name"] for item in items],
        "revision": binding_revision(entries) if entries else "",
    }


async def _worker_instance(
    environment: OpenCodeExecutionEnvironment,
) -> WorkerInstance | None:
    if getattr(environment, "id", None) is None:
        return None
    async with SessionLocal() as session:
        return await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment.id)
        )


def _expected_payload(desired: dict[str, Any]) -> dict[str, Any]:
    skills = desired.get("skills") if isinstance(desired.get("skills"), dict) else {}
    mcp = desired.get("mcp") if isinstance(desired.get("mcp"), dict) else {}
    model = desired.get("model") if isinstance(desired.get("model"), dict) else {}
    opencode = desired.get("opencode") if isinstance(desired.get("opencode"), dict) else {}
    return {
        "expected_skills": [
            str(item.get("name"))
            for item in skills.get("items") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "expected_mcp": [
            str(item.get("name"))
            for item in mcp.get("items") or []
            if isinstance(item, dict) and item.get("name")
        ],
        "expected_model": _text(model.get("name")),
        "expected_opencode_version": _text(opencode.get("version")),
    }


def _observation(
    result: Any,
    instance: WorkerInstance | None,
    observed_epoch: int | None,
    *,
    source: str,
) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    observation: dict[str, Any] = {}
    for key in REQUIRED_SECTIONS:
        section = payload.get(key)
        observation[key] = section if isinstance(section, dict) else _unknown_section("探测未返回该项")
    observation["checked_at"] = _text(payload.get("checked_at")) or _now()
    observation["worker_agent_version"] = (
        _text(instance.agent_version) if instance is not None else ""
    ) or None
    observation["observed_epoch"] = observed_epoch
    observation["source"] = source
    observation["error"] = None
    return _safe_data(observation)


def _failed_observation(
    reason: Any,
    instance: WorkerInstance | None,
    observed_epoch: int | None,
    *,
    source: str,
    not_deployed: bool = False,
) -> dict[str, Any]:
    message = _redact_text(reason) or "Worker 装备探测失败"
    if not_deployed and "未部署" not in message:
        message = f"Worker 未部署：{message}"
    observation: dict[str, Any] = {
        key: _unknown_section(message) for key in REQUIRED_SECTIONS
    }
    observation["checked_at"] = _now()
    observation["worker_agent_version"] = (
        _text(instance.agent_version) if instance is not None else ""
    ) or None
    observation["observed_epoch"] = observed_epoch
    observation["source"] = source
    observation["error"] = message
    return _safe_data(observation)


def _unknown_section(error: str) -> dict[str, Any]:
    return {"status": UNKNOWN, "error": error}


async def _probe_via_ssh(
    environment: OpenCodeExecutionEnvironment,
    expected: dict[str, Any],
    settings: Settings,
    perenv_python: str | None,
) -> dict[str, Any]:
    environment_id = int(environment.id)
    python = perenv_python or f"/opt/nexus/workers/{environment_id}/venv/bin/python"
    env_file = f"/etc/nexus/worker-{environment_id}.env"
    script = _ssh_inspection_script(env_file, expected)
    command = f"{shlex.quote(str(python))} -c {shlex.quote(script)}"
    session = None
    try:
        session = await ExecutionEnvironmentRuntime(settings).open_session(environment)
        code, out, err = await session.run_command(command)
    finally:
        if session is not None:
            await session.close()

    payload = _last_json(out)
    error_code = payload.get("error") if isinstance(payload, dict) else None
    if error_code == "worker_agent_missing" or code == 127:
        return {
            "failure": f"找不到每环境 Worker Agent Python（{python}）",
            "not_deployed": True,
        }
    if error_code == "worker_env_missing":
        return {
            "failure": f"缺少 Worker 环境文件（{env_file}）",
            "not_deployed": True,
        }
    if error_code == "worker_config_identity_missing":
        return {"failure": "Worker 环境文件缺少 OPENCODE_CONFIG/OPENCODE_CONFIG_DIR"}
    if code != 0:
        return {"failure": (err or out or f"远程探测退出码 {code}").strip()}
    if not isinstance(payload, dict):
        return {"failure": "Worker 探测没有返回有效 JSON"}
    return payload


def _ssh_inspection_script(env_file: str, expected: dict[str, Any]) -> str:
    payload = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
    allowed = ", ".join(repr(item) for item in _ENV_ALLOW)
    return f"""import json
import os

ENV_FILE = {env_file!r}
ALLOWED = {{{allowed}}}
try:
    with open(ENV_FILE, encoding="utf-8") as handle:
        for line in handle:
            key, sep, value = line.partition("=")
            key = key.strip()
            if sep and key in ALLOWED:
                os.environ[key] = value.strip().strip('"').strip("'")
except FileNotFoundError:
    print(json.dumps({{"error": "worker_env_missing"}}))
    raise SystemExit(4)
if not os.environ.get("OPENCODE_CONFIG") or not os.environ.get("OPENCODE_CONFIG_DIR"):
    print(json.dumps({{"error": "worker_config_identity_missing"}}))
    raise SystemExit(6)
try:
    from app.worker_agent.inspection import inspect_runtime
except ImportError:
    print(json.dumps({{"error": "worker_agent_missing"}}))
    raise SystemExit(5)
expected = json.loads({payload!r})
url = os.environ.get("NEXUS_WORKER_OPENCODE_URL") or ""
workspace = os.environ.get("NEXUS_WORKER_WORKSPACE_ROOT") or ""
result = inspect_runtime(
    url,
    workspace,
    expected_skills=expected["expected_skills"],
    expected_mcp=expected["expected_mcp"],
    expected_model=expected["expected_model"],
    expected_opencode_version=expected["expected_opencode_version"],
)
print(json.dumps(result, ensure_ascii=False))
"""


def _last_json(value: Any) -> dict[str, Any] | None:
    for line in reversed(str(value or "").splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        return payload if isinstance(payload, dict) else None
    return None


def _ssh_available(environment: OpenCodeExecutionEnvironment) -> bool:
    return bool(environment.host and environment.username and environment.ssh_password)


def _opencode_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    want = _text((desired.get("opencode") or {}).get("version"))
    section = _section(observation, "opencode")
    deployed_version = _text((snapshot.get("opencode") or {}).get("version"))
    actual = _text(section.get("version")) or deployed_version
    status = _status(section)
    deployed = bool(want) and deployed_version == want
    verified = _tri_state(status, bool(want) and actual == want)
    diff = _version_diff("OpenCode", want, actual, status)
    return _row(
        key="opencode",
        label="OpenCode",
        configured=bool(want),
        deployed=deployed,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=f"OpenCode {actual}" if actual else None,
    )


def _worker_agent_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    want = _text((desired.get("worker_agent") or {}).get("version"))
    deployed_version = _text((snapshot.get("worker_agent") or {}).get("version"))
    actual = _text(observation.get("worker_agent_version")) or deployed_version
    source = observation.get("source")
    deployed = bool(want) and deployed_version == want
    if source == "worker" and want and actual:
        verified: bool | None = _same_version(want, actual)
    else:
        verified = None
    diff = []
    if source == "worker" and want and actual and not _same_version(want, actual):
        diff.append(f"Worker Agent 版本不一致：期望 {want}，实际 {actual}")
    if source != "worker":
        diff.append("Worker Agent 未通过在线握手核验")
    return _row(
        key="worker_agent",
        label="Worker Agent",
        configured=bool(want),
        deployed=deployed,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=None,
        detail=f"Worker Agent {actual}" if actual else None,
    )


def _model_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    model = desired.get("model") if isinstance(desired.get("model"), dict) else {}
    want = _text(model.get("name"))
    section = _section(observation, "model")
    deployed_model = _text((snapshot.get("model") or {}).get("name"))
    actual = _text(section.get("model")) or deployed_model
    status = _status(section)
    deployed = bool(want) and deployed_model == want
    verified = _tri_state(status, bool(want) and actual == want)
    diff = _version_diff("模型", want, actual, status)
    if section.get("authenticated") is False and not diff:
        diff.append("模型网关认证失败")
    desired_error = _text(model.get("error"))
    if desired_error and not diff:
        diff.append(desired_error)
    return _row(
        key="model",
        label="模型",
        configured=bool(want),
        deployed=deployed,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual or None,
        diff=diff,
        error=_text(section.get("error")) or desired_error or None,
        detail=f"实际模型 {actual}" if actual else None,
    )


def _mcp_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    mcp = desired.get("mcp") if isinstance(desired.get("mcp"), dict) else {}
    items = [item for item in mcp.get("items") or [] if isinstance(item, dict)]
    want = _text(mcp.get("revision"))
    required = [str(name) for name in mcp.get("required") or [] if name]
    section = _section(observation, "mcp")
    status = _status(section)
    missing = [str(name) for name in section.get("missing") or [] if name]
    endpoints = section.get("endpoints")
    deployed_revision = _snapshot_revision(snapshot, "mcp")
    deployed = bool(want) and deployed_revision == want
    problems: list[str] = []
    unknown: list[str] = []
    if status == HEALTHY and not missing and isinstance(endpoints, dict):
        for item in items:
            name = _text(item.get("name"))
            want_url = _sanitize_url(item.get("url"))
            if not name or not want_url:
                continue
            if name not in endpoints:
                problems.append(f"{name} 端点未配置")
            elif _sanitize_url(endpoints.get(name)) != want_url:
                problems.append(f"{name} URL 不一致")
    elif status == HEALTHY and not missing:
        unknown.append("实际 MCP 端点未知（/config 不可读）")
    if status == UNHEALTHY or missing:
        verified: bool | None = False
    elif status == HEALTHY:
        if problems:
            verified = False
        elif unknown:
            verified = None
        else:
            verified = True
    else:
        verified = None
    diff = []
    if missing:
        diff.append("MCP 未连接：" + "、".join(missing))
    diff.extend(problems)
    diff.extend(unknown)
    if want and deployed_revision and deployed_revision != want:
        diff.append("MCP 配置 revision 与已部署记录不一致")
    if status == UNKNOWN:
        diff.append("MCP 未核验")
    actual_revision = _mcp_revision_from_endpoints(items, endpoints)
    return _row(
        key="mcp",
        label="MCP",
        configured=bool(required),
        deployed=deployed,
        verified=verified,
        desired_revision=want or None,
        actual_revision=actual_revision or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=(
            "已连接：" + "、".join(required)
            if status == HEALTHY and not missing
            else None
        ),
    )


def _skills_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    skills = desired.get("skills") if isinstance(desired.get("skills"), dict) else {}
    items = [item for item in skills.get("items") or [] if isinstance(item, dict)]
    want = _text(skills.get("revision"))
    want_content = _text(skills.get("content_revision")) or want
    section = _section(observation, "skills")
    status = _status(section)
    missing = [str(name) for name in section.get("missing") or [] if name]
    if status != UNKNOWN and "discovered" in section and not missing:
        discovered = {str(name) for name in section.get("discovered") or [] if name}
        missing = [str(item.get("name")) for item in items if item.get("name") not in discovered]
    content_hashes = section.get("content_hashes")
    actual_content_revision = ""
    managed_names = sorted({_text(item.get("name")) for item in items if _text(item.get("name"))})
    if isinstance(content_hashes, dict) and managed_names:
        managed_hashes = {
            name: _text(content_hashes.get(name)) for name in managed_names
        }
        if all(managed_hashes.values()):
            actual_content_revision = _skills_content_revision(
                [
                    {"name": name, "content_hash": managed_hashes[name]}
                    for name in managed_names
                ]
            )
    problems: list[str] = []
    unknown: list[str] = []
    if status == HEALTHY and not missing:
        if not isinstance(content_hashes, dict):
            unknown.append("实际 Skill 内容未知（/skill content 缺失）")
        else:
            for item in items:
                name = _text(item.get("name"))
                want_hash = _text(item.get("content_hash"))
                if not name:
                    continue
                if not want_hash:
                    unknown.append(f"{name} 期望内容不可核验")
                elif name not in content_hashes:
                    problems.append(f"{name} 未发现")
                elif not content_hashes.get(name):
                    unknown.append(f"{name} 实际内容未知")
                elif content_hashes.get(name) != want_hash:
                    problems.append(f"{name} 内容不一致")
    source_missing = [str(name) for name in skills.get("missing_sources") or [] if name]
    unverifiable = [str(name) for name in skills.get("unverifiable_sources") or [] if name]
    deployed_revision = _snapshot_revision(snapshot, "skills")
    deployed = bool(want) and deployed_revision == want
    if status == UNHEALTHY or missing or problems:
        verified: bool | None = False
    elif status == HEALTHY:
        verified = None if unknown else True
    else:
        verified = None
    diff = []
    if missing:
        diff.append("Skill 未发现：" + "、".join(missing))
    diff.extend(f"Skill {item}" for item in problems)
    diff.extend(f"Skill 未核验：{item}" for item in unknown)
    if source_missing:
        diff.append("Skill 源缺失：" + "、".join(source_missing))
    if unverifiable:
        diff.append("Skill 内容不可核验：" + "、".join(unverifiable))
    if want and deployed_revision and deployed_revision != want:
        diff.append("Skill 部署 revision 与已部署记录不一致")
    if status == UNKNOWN:
        diff.append("Skill 未核验")
    return _row(
        key="skills",
        label="Skill",
        configured=bool(items),
        deployed=deployed,
        verified=verified,
        desired_revision=want_content or None,
        actual_revision=actual_content_revision or None,
        diff=diff,
        error=_text(section.get("error")) or None,
        detail=(
            "发现 " + "、".join(str(item.get("name")) for item in items)
            if status == HEALTHY and not missing
            else None
        ),
    )


def _git_row(
    desired: dict[str, Any], observation: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    repository = desired.get("repository") if isinstance(desired.get("repository"), dict) else {}
    section = _section(observation, "repository")
    status = _status(section)
    want_revision = _repository_revision(repository) if repository else ""
    deployed_repository = (
        snapshot.get("repository") if isinstance(snapshot.get("repository"), dict) else {}
    )
    deployed_revision = _repository_revision(deployed_repository) if deployed_repository else ""
    expected_url = _sanitize_url(repository.get("url"))
    actual_url = _sanitize_url(section.get("url"))
    expected_branch = _text(repository.get("target_branch"))
    actual_branch = _text(section.get("branch"))
    commit = _text(section.get("commit"))
    matches = (
        bool(repository)
        and status == HEALTHY
        and bool(expected_url)
        and actual_url == expected_url
        and bool(expected_branch)
        and actual_branch == expected_branch
        and bool(commit)
    )
    deployed = bool(deployed_revision) and deployed_revision == want_revision
    if not repository:
        verified: bool | None = None
    elif status == UNHEALTHY:
        verified = False
    elif status == HEALTHY:
        if not matches:
            verified = False
        elif repository.get("authentication") == "token":
            verified = True if section.get("authentication_verified") is True else None
        else:
            verified = True
    else:
        verified = None
    diff = []
    if not repository:
        diff.append("未绑定 Git 仓库，资源不能调度")
    else:
        if status == UNKNOWN:
            diff.append("Git 仓库未核验")
        elif status == UNHEALTHY:
            diff.append("Git 仓库探测失败")
        elif expected_url and actual_url and expected_url != actual_url:
            diff.append("仓库地址与期望不一致")
        if expected_branch and actual_branch and expected_branch != actual_branch:
            diff.append(f"分支不一致：期望 {expected_branch}，实际 {actual_branch}")
        if status == HEALTHY and not commit:
            diff.append("未检测到有效 HEAD")
        if section.get("dirty") is True:
            diff.append("工作区存在未提交变更（仅阻止更新，不影响运行）")
        if repository.get("authentication") == "token" and section.get("authentication_verified") is not True:
            diff.append("私有仓库认证未单独探测")
        if want_revision and deployed_revision and deployed_revision != want_revision:
            diff.append("Git 配置 revision 与已部署记录不一致")
        desired_error = _text(repository.get("error"))
        if desired_error:
            diff.append(desired_error)
    error = _text(section.get("error")) or _text(repository.get("error"))
    if not repository and not error:
        error = "未绑定 Git 仓库"
    return _row(
        key="git",
        label="Git 仓库",
        configured=bool(repository),
        deployed=deployed,
        verified=verified,
        desired_revision=want_revision or None,
        actual_revision=commit or None,
        diff=diff,
        error=error or None,
        detail=(
            f"{actual_branch}@{commit[:12]}" if actual_branch and commit else None
        ),
    )


def _row(
    *,
    key: str,
    label: str,
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
        "configured": bool(configured),
        "deployed": bool(deployed),
        "verified": verified,
        "desired_revision": desired_revision,
        "actual_revision": actual_revision,
        "diff": [_redact_text(item) for item in diff if item],
        "error": _redact_text(error) if error else None,
        "detail": _redact_text(detail) if detail else None,
    }


def _section(observation: dict[str, Any], key: str) -> dict[str, Any]:
    section = observation.get(key)
    return section if isinstance(section, dict) else {}


def _status(section: dict[str, Any]) -> str:
    return _text(section.get("status")) or UNKNOWN


def _tri_state(status: str, matches: bool) -> bool | None:
    if status == HEALTHY:
        return True if matches else False
    if status == UNHEALTHY:
        return False
    return None


def _version_diff(label: str, want: str, actual: str, status: str) -> list[str]:
    if want and actual and not _same_version(want, actual):
        return [f"{label}不一致：期望 {want}，实际 {actual}"]
    if status == UNHEALTHY:
        return [f"{label}健康探测失败"]
    if status == UNKNOWN:
        return [f"{label}未核验"]
    return []


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


def _mcp_revision_from_endpoints(
    items: list[dict[str, Any]], endpoints: Any
) -> str:
    if not isinstance(endpoints, dict):
        return ""
    rows = []
    for item in items:
        name = _text(item.get("name"))
        if not name:
            continue
        rows.append({"name": name, "url": _sanitize_url(endpoints.get(name))})
    entries = binding_entries(None, rows)
    return binding_revision(entries) if entries else ""


def _snapshot_revision(snapshot: dict[str, Any], key: str) -> str:
    section = snapshot.get(key)
    if not isinstance(section, dict):
        return ""
    revision = _text(section.get("revision"))
    if revision:
        return revision
    items = section.get("items")
    if not isinstance(items, list):
        return ""
    if key == "mcp":
        entries = binding_entries(None, [item for item in items if isinstance(item, dict)])
        return binding_revision(entries) if entries else ""
    if key == "skills":
        return _skills_revision([item for item in items if isinstance(item, dict)])
    return ""


def _repository_revision(repository: dict[str, Any]) -> str:
    if not repository:
        return ""
    return _revision(
        {
            "url": _sanitize_url(repository.get("url")),
            "target_branch": repository.get("target_branch"),
            "authentication": repository.get("authentication"),
            "credential_revision": repository.get("credential_revision"),
        }
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _revision(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _sanitize_url(value: Any) -> str:
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


def _redact_text(value: Any) -> str:
    return redact_text(value)


def _safe_data(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {str(key): _safe_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_data(item) for item in value]
    return redact_data(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _now() -> str:
    return datetime.now(UTC).isoformat()
