"""按需只读运行探测辅助（供显式 check / deploy 对账复用），就绪事实源统一委托 ``worker_readiness``。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.config.settings import Settings, get_settings
from app.database import SessionLocal
from app.models import AgentWorker
from app.runtime.redaction import redact_data, redact_text
from app.runtime.worker_client import WorkerOpenCodeClient

REQUIRED_SECTIONS = ("opencode", "model", "mcp", "skills", "repository")


def expected_inspection_payload(desired: dict[str, Any], *, deep: bool = False) -> dict[str, Any]:
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
        "expected_model": str(model.get("name") or "").strip(),
        "expected_opencode_version": str(opencode.get("version") or "").strip(),
        "deep": bool(deep),
    }


async def probe_online_worker(
    environment: AgentWorker,
    desired: dict[str, Any],
    settings: Settings | None = None,
    *,
    deep: bool = False,
) -> dict[str, Any]:
    """对在线节点发起按需只读诊断（deep=True 时在节点侧按需校验远端 Git 与网关连通性）。"""

    resolved = settings or get_settings()
    async with SessionLocal() as session:
        worker = await session.get(AgentWorker, environment.id)
    if worker is None or worker.status not in {"IDLE", "WORKING", "PAUSED"}:
        return _failed_observation("Worker 通道离线，无法执行在线诊断", worker)
    payload = expected_inspection_payload(desired, deep=deep)
    try:
        client = WorkerOpenCodeClient(resolved, agent_worker_id=environment.id)
        result = await client.run_command("inspect_runtime", payload)
    except Exception as exc:  # noqa: BLE001
        return _failed_observation(exc, worker)
    return normalize_probe_result(result, worker)


def normalize_probe_result(
    result: Any,
    worker: AgentWorker | None = None,
    *,
    source: str = "worker",
) -> dict[str, Any]:
    payload = result if isinstance(result, dict) else {}
    observation: dict[str, Any] = {}
    for key in REQUIRED_SECTIONS:
        section = payload.get(key)
        observation[key] = (
            section if isinstance(section, dict) else {"status": "unknown", "error": "探测未返回该项"}
        )
    observation["checked_at"] = str(payload.get("checked_at") or "").strip() or datetime.now(UTC).isoformat()
    observation["worker_agent_version"] = (
        (str(worker.agent_version or "").strip() if worker is not None else "")
        or str(payload.get("worker_agent_version") or "").strip()
        or None
    )
    observation["observed_epoch"] = worker.epoch if worker is not None else None
    observation["source"] = source
    observation["spec_revision"] = payload.get("spec_revision")
    observation["error"] = str(payload.get("error") or "").strip() or None
    return redact_data(observation)


def _failed_observation(reason: Any, worker: AgentWorker | None) -> dict[str, Any]:
    message = redact_text(reason) or "Worker 装备探测失败"
    observation: dict[str, Any] = {key: {"status": "unknown", "error": message} for key in REQUIRED_SECTIONS}
    observation["checked_at"] = datetime.now(UTC).isoformat()
    observation["worker_agent_version"] = (
        str(worker.agent_version or "").strip() if worker is not None else None
    )
    observation["observed_epoch"] = worker.epoch if worker is not None else None
    observation["source"] = "worker"
    observation["error"] = message
    return redact_data(observation)
