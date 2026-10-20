"""能力绑定的 desired/applied。不新建表，也不改正在运行的任务。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def binding_entries(
    skill_refs: list[str] | None,
    mcp_refs: list[dict[str, Any]] | None,
) -> list[Any]:
    """修订号只算安装脚本会写入的字段：技能名，以及带 URL 的 MCP 名和 URL。"""

    entries: list[Any] = [str(name) for name in (skill_refs or []) if name]
    for item in mcp_refs or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        url = str(item.get("url") or "")
        if url:
            entries.append({"name": str(item["name"]), "url": url})
        else:
            entries.append(str(item["name"]))
    return entries


def binding_revision(entries: list[Any]) -> str:
    textual = [item for item in entries if isinstance(item, str) and item]
    structured = [item for item in entries if isinstance(item, dict) and item.get("name")]
    if not structured:
        payload = json.dumps(sorted(set(textual)), ensure_ascii=False, separators=(",", ":"))
    else:
        rows = [{"name": name} for name in textual]
        for item in structured:
            config = item.get("config")
            rows.append(
                {
                    "name": str(item["name"]),
                    "url": str(item.get("url") or ""),
                    "config": config if isinstance(config, dict) else {},
                }
            )
        rows.sort(
            key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def stored_capability(status_detail: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(status_detail, dict):
        return None
    record = status_detail.get("capability")
    return record if isinstance(record, dict) else None


def loads_jsonc(text: str) -> dict[str, Any]:
    from app.worker_agent.jsonc import loads_jsonc as parse_jsonc

    data = parse_jsonc(text)
    return data if isinstance(data, dict) else {}


def binding_names(skill_refs: list[str] | None, mcp_refs: list[dict[str, Any]] | None) -> list[str]:
    names = [str(name) for name in (skill_refs or []) if name]
    for item in mcp_refs or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return names


def stamp_desired(
    status_detail: dict[str, Any] | None,
    entries: list[Any],
    *,
    previous: dict[str, Any] | None = None,
    checked_at: str,
) -> dict[str, Any]:
    """绑定变更只写 desired。applied 留到部署发现之后。"""

    detail = dict(status_detail or {})
    source = previous if isinstance(previous, dict) else {}
    record = dict(source.get("capability") or {}) if isinstance(source.get("capability"), dict) else {}
    desired = binding_revision(entries) if entries else ""
    record["desired_revision"] = desired
    record["checked_at"] = checked_at
    if entries and record.get("applied_revision") != desired:
        record["runtime_status"] = "pending"
        record["error"] = ""
    elif not entries:
        record["runtime_status"] = "applied"
        record["error"] = ""
    detail["capability"] = record
    return detail


def skill_names_in(root: Path) -> set[str]:
    """OpenCode 1.18.35 在给定目录下找 `<name>/SKILL.md`。不扫描仓库里的 `.opencode/`。"""

    if not root.is_dir():
        return set()
    return {path.parent.name for path in root.glob("*/SKILL.md") if path.is_file()}


def mcp_names_in(config: dict[str, Any]) -> set[str]:
    raw = config.get("mcp")
    if not isinstance(raw, dict):
        return set()
    return {str(name) for name in raw if name}


def discover_worker(skill_roots: list[Path], config_text: str) -> set[str]:
    found: set[str] = set()
    for root in skill_roots:
        found.update(skill_names_in(root))
    found.update(mcp_names_in(loads_jsonc(config_text)))
    return found


def discover_names(skill_names: list[str], config_text: str) -> set[str]:
    """部署回读：技能目录名加上配置里的 MCP 名。"""

    return {name for name in skill_names if name} | mcp_names_in(loads_jsonc(config_text))


def observe_bindings(
    current: dict[str, Any] | None,
    *,
    desired: str,
    found: set[str],
    expected: list[str],
    error: str,
    checked_at: str,
) -> dict[str, Any]:
    record = dict(current or {})
    previous = str(record.get("applied_revision") or "")
    record["desired_revision"] = desired
    record["checked_at"] = checked_at
    if error:
        record["runtime_status"] = "error"
        record["error"] = error
        record["applied_revision"] = previous
        return record
    missing = [name for name in expected if name not in found]
    if missing:
        record["runtime_status"] = "error"
        record["error"] = "未发现：" + "、".join(missing)
        record["applied_revision"] = previous
        return record
    record["applied_revision"] = desired
    record["runtime_status"] = "applied"
    record["error"] = ""
    return record


def write_capability(status_detail: dict[str, Any] | None, record: dict[str, Any]) -> dict[str, Any]:
    detail = dict(status_detail or {})
    detail["capability"] = record
    return detail


def dispatch_allowed(
    status_detail: dict[str, Any] | None,
    *,
    bindings: list[str] | None = None,
) -> bool:
    """有绑定却没有已应用的修订记录时，不允许分配。"""

    names = [name for name in (bindings or []) if name]
    record = status_detail.get("capability") if isinstance(status_detail, dict) else None
    if not isinstance(record, dict):
        return not names
    desired = str(record.get("desired_revision") or "")
    if not desired:
        return not names
    return record.get("applied_revision") == desired and record.get("runtime_status") != "error"
