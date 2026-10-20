"""Nexus 自建 Skill/MCP 资源与 AgentScope 同步。

Nexus 只保存资源目录、版本和员工绑定；Skill 文件上传、MCP 连接与 Tool 发现
全部走 AgentScope 2.0.10 的 Workspace API，不复制它的加载器和 MCP 客户端。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from cryptography.fernet import Fernet, InvalidToken

from .agentscope import AgentScopeClient, AgentScopeError
from .db import employee_from_row, new_id

SKILL_ID_PREFIX = "nsk_"
MCP_ID_PREFIX = "nmp_"
TASK_SKILL_ID = "task-management"
TASK_SKILL_NAME = "任务管理"
MAX_SKILL_FILES = 100
MAX_SKILL_FILE_BYTES = 50 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 500 * 1024 * 1024
SOURCE_NEXUS = "nexus"
SOURCE_AGENTSCOPE = "agentscope"
SOURCE_BUILTIN = "builtin"
MCP_TRANSPORT_HTTP = "http"
MCP_AUTH_NONE = "none"
MCP_AUTH_BEARER = "bearer"
MCP_AUTH_HEADER = "header"
UNATTENDED_PERMISSION = "dont_ask"
logger = logging.getLogger("nexus.resources")


class ResourceError(Exception):
    def __init__(self, detail: str, status_code: int = 422) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def lines(values: Iterable[Any]) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def is_nexus_skill(ref: str) -> bool:
    return str(ref).startswith(SKILL_ID_PREFIX)


def is_nexus_mcp(ref: str) -> bool:
    return str(ref).startswith(MCP_ID_PREFIX)


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def employee_session_ids(connection, employee: dict[str, Any]) -> list[str]:
    """员工邮箱、私聊和团队会话。同步与状态回查共用这一份口径。"""

    found: list[str] = []

    def add(value: str | None) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text)

    add(employee.get("mailbox_session_id"))
    for row in connection.execute(
        "SELECT agentscope_session_id FROM conversations WHERE employee_id = ?",
        (employee["id"],),
    ).fetchall():
        add(row["agentscope_session_id"])
    for row in connection.execute(
        "SELECT agentscope_session_id FROM channel_members WHERE employee_id = ?",
        (employee["id"],),
    ).fetchall():
        add(row["agentscope_session_id"])
    return found


def all_employees(connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT * FROM employees ORDER BY created_at").fetchall()
    return [employee_from_row(row) for row in rows]


def employees_for(employees: list[dict[str, Any]], field: str, ref: str) -> list[dict[str, Any]]:
    return [item for item in employees if ref in (item.get(field) or [])]


def employee_brief(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"id": item["id"], "name": item["name"], "enabled": bool(item.get("enabled"))}
        for item in items
    ]


# ---------------------------------------------------------------------------
# Skill 文件与格式
# ---------------------------------------------------------------------------


def _safe_relative(path: str) -> str:
    raw = str(path).replace("\\", "/").strip()
    if raw.startswith("/") or re.match(r"^[a-zA-Z]:", raw):
        raise ResourceError(f"不安全的文件路径：{path}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ResourceError(f"不安全的文件路径：{path}")
    return "/".join(parts)


def normalize_uploads(entries: list[tuple[str, bytes]]) -> dict[str, bytes]:
    """把浏览器目录上传整理成相对技能根目录的文件集合。"""

    cleaned: dict[str, bytes] = {}
    for raw_path, data in entries:
        path = str(raw_path).replace("\\", "/").strip()
        if not path or path.endswith("/.DS_Store") or path.startswith("__MACOSX/"):
            continue
        if path.split("/")[-1] in {".DS_Store", "Thumbs.db"}:
            continue
        safe = _safe_relative(path)
        if safe in cleaned:
            raise ResourceError(f"上传目录里有重复文件：{safe}")
        cleaned[safe] = data
    if not cleaned:
        raise ResourceError("上传内容为空")
    if "SKILL.md" not in cleaned:
        roots = {path.split("/", 1)[0] for path in cleaned if "/" in path}
        if len(roots) == 1:
            root = next(iter(roots))
            if f"{root}/SKILL.md" in cleaned:
                cleaned = {
                    path[len(root) + 1 :]: data
                    for path, data in cleaned.items()
                    if path.startswith(f"{root}/")
                }
    if "SKILL.md" not in cleaned:
        raise ResourceError("技能目录根目录必须包含 SKILL.md")
    return cleaned


FRONTMATTER = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.S)


def parse_skill_markdown(data: bytes) -> tuple[str, str, str]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ResourceError("SKILL.md 必须是 UTF-8 文本") from exc
    match = FRONTMATTER.match(text)
    if match is None:
        raise ResourceError("SKILL.md 必须以 YAML frontmatter 开头，并包含 name 和 description")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ResourceError(f"SKILL.md frontmatter 不是合法 YAML：{exc}") from exc
    if not isinstance(meta, dict):
        raise ResourceError("SKILL.md frontmatter 必须是对象")
    name = str(meta.get("name") or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise ResourceError("SKILL.md frontmatter 缺少 name 或 description")
    return name, description, text


def validate_skill_files(files: dict[str, bytes]) -> tuple[str, str]:
    if not files:
        raise ResourceError("技能至少包含一个 SKILL.md")
    if "SKILL.md" not in files:
        raise ResourceError("技能目录根目录必须包含 SKILL.md")
    if len(files) > MAX_SKILL_FILES:
        raise ResourceError(f"一个技能最多 {MAX_SKILL_FILES} 个文件")
    total = 0
    for path, data in files.items():
        if len(data) > MAX_SKILL_FILE_BYTES:
            raise ResourceError(f"{path} 超过单文件 50MB 限制")
        total += len(data)
    if total > MAX_SKILL_TOTAL_BYTES:
        raise ResourceError("技能总大小超过 500MB 限制")
    name, description, _text = parse_skill_markdown(files["SKILL.md"])
    return name, description


def file_metadata(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return [{"path": path, "size": len(data)} for path, data in sorted(files.items())]


def file_payloads(files: dict[str, bytes]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for path, data in sorted(files.items()):
        item: dict[str, Any] = {"path": path, "size": len(data)}
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text and "\x00" not in text:
            item["content"] = text
            item["encoding"] = "utf8"
        else:
            item["content"] = base64.b64encode(data).decode("ascii")
            item["encoding"] = "base64"
        payloads.append(item)
    return payloads


class ResourceStore:
    """Nexus 自建 Skill 文件、版本和 MCP 凭据的本地存储。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.skills_root = root / "skills"
        self._key_path = root / ".mcp_master_key"
        self._fernet: Fernet | None = None

    # -- Skill 文件 -------------------------------------------------------
    def version_dir(self, skill_id: str, version: int) -> Path:
        return self.skills_root / skill_id / str(version)

    def write_version(self, skill_id: str, version: int, files: dict[str, bytes]) -> None:
        target = self.version_dir(skill_id, version)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        base = target.resolve()
        for path, data in files.items():
            destination = (target / path).resolve()
            if base not in destination.parents and destination != base:
                raise ResourceError(f"不安全的文件路径：{path}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

    def read_version_files(self, skill_id: str, version: int) -> dict[str, bytes]:
        directory = self.version_dir(skill_id, version)
        if not directory.is_dir():
            raise ResourceError("技能文件不存在，可能已被删除", 404)
        files: dict[str, bytes] = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                files[path.relative_to(directory).as_posix()] = path.read_bytes()
        return files

    def delete_skill_files(self, skill_id: str) -> None:
        target = self.skills_root / skill_id
        if target.exists():
            shutil.rmtree(target)

    # -- MCP 凭据 ---------------------------------------------------------
    def _cipher(self) -> Fernet:
        if self._fernet is not None:
            return self._fernet
        raw = os.environ.get("NEXUS_MCP_MASTER_KEY", "").strip()
        if not raw:
            raw = self._load_or_create_key()
        try:
            self._fernet = Fernet(raw.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ResourceError("NEXUS_MCP_MASTER_KEY 不是合法的 Fernet 主密钥", 500) from exc
        return self._fernet

    def _load_or_create_key(self) -> str:
        if self._key_path.exists():
            return self._key_path.read_text(encoding="ascii").strip()
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key().decode("ascii")
        self._key_path.write_text(key, encoding="ascii")
        try:
            os.chmod(self._key_path, 0o600)
        except OSError:
            pass
        return key

    def encrypt_secret(self, value: str) -> str:
        if not value:
            return ""
        return self._cipher().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_secret(self, value: str) -> str:
        if not value:
            return ""
        try:
            return self._cipher().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ResourceError("MCP 凭据无法用当前主密钥解密", 500) from exc

    # -- 设置 -------------------------------------------------------------
    def get_setting(self, connection, key: str) -> str:
        row = connection.execute("SELECT value FROM nexus_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else ""

    def set_setting(self, connection, key: str, value: str) -> None:
        connection.execute(
            "INSERT INTO nexus_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        connection.commit()


# ---------------------------------------------------------------------------
# Skill 行与目录
# ---------------------------------------------------------------------------


def skill_row(connection, skill_id: str):
    return connection.execute("SELECT * FROM nexus_skills WHERE id = ?", (skill_id,)).fetchone()


def mcp_row(connection, mcp_id: str):
    return connection.execute("SELECT * FROM nexus_mcps WHERE id = ?", (mcp_id,)).fetchone()


def local_skill_rows(connection, skill_ids: Iterable[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for skill_id in skill_ids:
        if not is_nexus_skill(skill_id):
            continue
        row = skill_row(connection, skill_id)
        if row is not None:
            found[skill_id] = row
    return found


def local_mcp_rows(connection, mcp_ids: Iterable[str]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for mcp_id in mcp_ids:
        if not is_nexus_mcp(mcp_id):
            continue
        row = mcp_row(connection, mcp_id)
        if row is not None:
            found[mcp_id] = row
    return found


async def external_catalogs(
    client: AgentScopeClient,
    *,
    need_skills: bool = True,
    need_mcps: bool = True,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    skills: dict[str, dict[str, Any]] = {}
    mcps: dict[str, dict[str, Any]] = {}
    error = ""
    if not client.configured:
        return skills, mcps, "AgentScope 未连接"
    if need_skills:
        try:
            for item in await client.list_skill_records():
                item_id = str(item.get("id") or item.get("skill_id") or "")
                if item_id:
                    skills[item_id] = item
        except AgentScopeError as exc:
            error = exc.detail
    if need_mcps:
        try:
            for item in await client.list_mcp_records():
                item_id = str(item.get("id") or "")
                if item_id:
                    mcps[item_id] = item
        except AgentScopeError as exc:
            error = exc.detail
    return skills, mcps, error


def _skill_record_name(record: dict[str, Any], fallback: str) -> str:
    return str(record.get("name") or record.get("display_name") or fallback)


def skill_sync_summary(
    connection,
    skill_id: str,
    revision: int,
    enabled: bool,
    bound: list[dict[str, Any]],
) -> dict[str, Any]:
    return _sync_summary(connection, "skill", skill_id, revision, enabled, bound)


def mcp_sync_summary(
    connection,
    mcp_id: str,
    revision: int,
    enabled: bool,
    bound: list[dict[str, Any]],
) -> dict[str, Any]:
    return _sync_summary(connection, "mcp", mcp_id, revision, enabled, bound)


def _sync_summary(
    connection,
    resource_type: str,
    resource_id: str,
    revision: int,
    enabled: bool,
    bound: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = {
        "status": "unbound",
        "detail": "",
        "applied": 0,
        "pending": 0,
        "error": 0,
        "last_synced_at": "",
    }
    if not enabled:
        summary["status"] = "disabled"
        summary["detail"] = "资源已停用，运行环境不装载"
        return summary
    if not bound:
        return summary
    applied = pending = errors = 0
    details: list[str] = []
    latest = ""
    for employee in bound:
        sessions = employee_session_ids(connection, employee)
        if not sessions:
            pending += 1
            details.append(f"{employee['name']}：尚未创建会话")
            continue
        for session_id in sessions:
            row = connection.execute(
                """
                SELECT status, detail, revision, updated_at FROM resource_sync
                WHERE resource_type = ? AND resource_id = ? AND employee_id = ? AND session_id = ?
                """,
                (resource_type, resource_id, employee["id"], session_id),
            ).fetchone()
            if row is None:
                pending += 1
                details.append(f"{employee['name']}：待同步")
                continue
            if row["updated_at"] > latest:
                latest = str(row["updated_at"])
            status = str(row["status"])
            if status == "error":
                errors += 1
                if row["detail"]:
                    details.append(f"{employee['name']}：{row['detail']}")
            elif status != "applied" or int(row["revision"] or 0) != int(revision):
                pending += 1
                details.append(f"{employee['name']}：待同步")
            else:
                applied += 1
    if errors:
        summary["status"] = "error"
    elif pending:
        summary["status"] = "pending"
    else:
        summary["status"] = "applied"
    summary.update(
        applied=applied,
        pending=pending,
        error=errors,
        detail="；".join(dict.fromkeys(details))[:600],
        last_synced_at=latest,
    )
    return summary


def skill_payload(row, bound: list[dict[str, Any]], sync: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "source": SOURCE_NEXUS,
        "version": int(row["version"]),
        "revision": int(row["revision"]),
        "runtime_name": row["runtime_name"],
        "files": _json_list(row["files_json"]),
        "enabled": bool(row["enabled"]),
        "editable": True,
        "employees": employee_brief(bound),
        "sync": sync,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def external_skill_payload(
    record: dict[str, Any],
    bound: list[dict[str, Any]],
    sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    skill_id = str(record.get("id") or record.get("skill_id") or "")
    record_name = _skill_record_name(record, skill_id)
    return {
        "id": skill_id,
        "name": record_name,
        "description": str(record.get("description") or ""),
        "source": SOURCE_AGENTSCOPE,
        "version": str(record.get("version") or ""),
        "revision": 0,
        "runtime_name": record_name,
        "files": [],
        "enabled": bool(record.get("enabled", True)),
        "editable": False,
        "employees": employee_brief(bound),
        "sync": sync
        or {
            "status": "applied" if bound else "unbound",
            "detail": "",
            "applied": len(bound),
            "pending": 0,
            "error": 0,
            "last_synced_at": "",
        },
    }


def builtin_skill_payload(bound: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": TASK_SKILL_ID,
        "name": TASK_SKILL_NAME,
        "description": "把需要改代码、跑工程或交付工作区结果的请求做成 Nexus 任务",
        "source": SOURCE_BUILTIN,
        "version": "内置",
        "revision": 0,
        "runtime_name": TASK_SKILL_NAME,
        "files": [],
        "enabled": True,
        "editable": False,
        "employees": employee_brief(bound),
        "sync": {
            "status": "applied" if bound else "unbound",
            "detail": "由任务能力挂载逻辑维护",
            "applied": len(bound),
            "pending": 0,
            "error": 0,
            "last_synced_at": "",
        },
    }


def mcp_payload(row, bound: list[dict[str, Any]], sync: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "source": SOURCE_NEXUS,
        "url": row["url"],
        "transport": row["transport"],
        "headers": _json_list(row["headers_json"]),
        "auth_type": row["auth_type"],
        "auth_header": row["auth_header"],
        "has_token": bool(row["token_ciphertext"]),
        "timeout": float(row["timeout"]),
        "runtime_name": row["runtime_name"],
        "enabled": bool(row["enabled"]),
        "revision": int(row["revision"]),
        "editable": True,
        "employees": employee_brief(bound),
        "sync": sync,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def external_mcp_payload(
    record: dict[str, Any],
    bound: list[dict[str, Any]],
    runtime_sync: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mcp_id = str(record.get("id") or "")
    record_name = str(record.get("name") or mcp_id)
    return {
        "id": mcp_id,
        "name": record_name,
        "description": str(record.get("description") or ""),
        "source": SOURCE_AGENTSCOPE,
        "url": "",
        "transport": "agent-scope",
        "headers": [],
        "auth_type": "managed",
        "auth_header": "",
        "has_token": False,
        "timeout": 0,
        "runtime_name": record_name,
        "enabled": bool(record.get("enabled", True)),
        "revision": 0,
        "editable": False,
        "employees": employee_brief(bound),
        "sync": runtime_sync
        or {
            "status": "applied" if bound else "unbound",
            "detail": "",
            "applied": len(bound),
            "pending": 0,
            "error": 0,
            "last_synced_at": "",
        },
    }


def validate_mcp_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ResourceError("MCP 地址必须是 http 或 https URL")
    if not parsed.hostname:
        raise ResourceError("MCP 地址缺少主机名")
    if parsed.username or parsed.password:
        raise ResourceError("MCP 地址不能内嵌账号密码，请使用鉴权配置")
    return value


def normalize_headers(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    headers: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "")
        if not key:
            continue
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", key):
            raise ResourceError(f"请求头名称不合法：{key}")
        if key.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
            raise ResourceError(f"请求头 {key} 由 AgentScope 管理，不能自定义")
        if key.lower() in seen:
            raise ResourceError(f"请求头重复：{key}")
        seen.add(key.lower())
        headers.append({"key": key, "value": value})
    return headers


def mcp_runtime_name(name: str, mcp_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:24] or "mcp"
    return f"nexus_{slug}_{mcp_id[-8:]}"


def _slug_for_skill(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_")[:40] or "skill"


# ---------------------------------------------------------------------------
# 同步
# ---------------------------------------------------------------------------


def _set_sync(
    connection,
    resource_type: str,
    resource_id: str,
    employee_id: str,
    session_id: str,
    *,
    status: str,
    detail: str,
    revision: int,
) -> None:
    connection.execute(
        """
        INSERT INTO resource_sync (
            resource_type, resource_id, employee_id, session_id, status, detail, revision, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(resource_type, resource_id, employee_id, session_id)
        DO UPDATE SET status = excluded.status, detail = excluded.detail,
                      revision = excluded.revision, updated_at = excluded.updated_at
        """,
        (resource_type, resource_id, employee_id, session_id, status, detail, revision, utcnow()),
    )


def _installed_names(items: list[dict[str, Any]]) -> set[str]:
    return {str(item.get("name") or "").strip() for item in items if str(item.get("name") or "").strip()}


def _matching_skill_names(name: str, installed: set[str]) -> list[str]:
    """AgentScope 同名技能会加 “ (2)” 后缀，收回时一并处理。"""

    if not name:
        return []
    return sorted(
        item for item in installed if item == name or item.startswith(f"{name} (")
    )


def _mark_all(
    connection,
    refs: Iterable[str],
    *,
    employee_id: str,
    sessions: list[str],
    status: str,
    detail: str,
    revisions: dict[str, int],
) -> None:
    for ref in refs:
        resource_type = "skill" if is_nexus_skill(ref) else "mcp"
        revision = revisions.get(ref, 0)
        for session_id in sessions:
            _set_sync(
                connection,
                resource_type,
                ref,
                employee_id,
                session_id,
                status=status,
                detail=detail,
                revision=revision,
            )
    connection.commit()


async def sync_employee_resources(
    client: AgentScopeClient,
    employee: dict[str, Any],
    sessions: list[str],
    connection,
    store: ResourceStore,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把员工绑定的 Skill/MCP 收敛到指定会话，并记录逐会话状态。

    这是唯一同步入口：员工保存、会话创建、发消息前修复和手动重试都调用它。
    失败不会删除 Nexus 配置，只写 error/pending 供页面回查。
    """

    sessions = [item for item in lines(sessions)]
    desired_skills = lines(employee.get("skills") or [])
    desired_mcps = lines(employee.get("mcps") or [])
    previous = previous or {}
    previous_skills = lines(previous.get("skills") or [])
    previous_mcps = lines(previous.get("mcps") or [])
    touched_skills = list(dict.fromkeys([*desired_skills, *previous_skills]))
    touched_mcps = list(dict.fromkeys([*desired_mcps, *previous_mcps]))
    local_skills = local_skill_rows(connection, touched_skills)
    local_mcps = local_mcp_rows(connection, touched_mcps)
    revisions: dict[str, int] = {}
    for skill_id, row in local_skills.items():
        revisions[skill_id] = int(row["revision"])
    for mcp_id, row in local_mcps.items():
        revisions[mcp_id] = int(row["revision"])

    agent_id = str(employee.get("agentscope_agent_id") or "")
    if not sessions:
        return {"ok": True, "status": "pending", "detail": "员工还没有可同步的会话", "sessions": 0}
    if not client.configured or not agent_id:
        detail = "AgentScope 未连接，配置已保留" if not client.configured else "员工还没有 Agent"
        _mark_all(
            connection,
            [*touched_skills, *touched_mcps],
            employee_id=employee["id"],
            sessions=sessions,
            status="pending",
            detail=detail,
            revisions=revisions,
        )
        return {"ok": False, "status": "pending", "detail": detail, "sessions": len(sessions)}

    external_skill_refs = [item for item in touched_skills if not is_nexus_skill(item)]
    external_mcp_refs = [item for item in touched_mcps if not is_nexus_mcp(item)]
    external_skills, external_mcps, catalog_error = await external_catalogs(
        client,
        need_skills=bool(external_skill_refs),
        need_mcps=bool(external_mcp_refs),
    )
    errors: list[str] = []
    pending: list[str] = []
    applied_sessions = 0

    for session_id in sessions:
        try:
            result = await _sync_one_session(
                client,
                employee,
                session_id,
                connection,
                store=store,
                desired_skills=desired_skills,
                desired_mcps=desired_mcps,
                previous_skills=previous_skills,
                previous_mcps=previous_mcps,
                local_skills=local_skills,
                local_mcps=local_mcps,
                external_skills=external_skills,
                external_mcps=external_mcps,
                catalog_error=catalog_error,
            )
        except (AgentScopeError, ResourceError) as exc:
            detail = _redact(
                getattr(exc, "detail", str(exc)) or "AgentScope 同步失败",
                _secret_values(local_mcps.values(), store),
            )
            _mark_all(
                connection,
                [*touched_skills, *touched_mcps],
                employee_id=employee["id"],
                sessions=[session_id],
                status="error",
                detail=detail,
                revisions=revisions,
            )
            errors.append(detail)
            continue
        if result["status"] == "running":
            pending.append(result["detail"])
            continue
        if result["status"] == "error":
            errors.append(result["detail"])
            continue
        applied_sessions += 1

    if errors:
        status = "error"
        detail = "；".join(dict.fromkeys(errors))[:800]
    elif pending:
        status = "pending"
        detail = "；".join(dict.fromkeys(pending))[:800]
    else:
        status = "applied"
        detail = ""
    return {
        "ok": status == "applied",
        "status": status,
        "detail": detail,
        "sessions": len(sessions),
        "applied_sessions": applied_sessions,
    }


__all__ = ["ResourceStore", "ResourceError"]


# ---------------------------------------------------------------------------
# 单会话收敛
# ---------------------------------------------------------------------------


async def _sync_one_session(
    client: AgentScopeClient,
    employee: dict[str, Any],
    session_id: str,
    connection,
    *,
    store: ResourceStore,
    desired_skills: list[str],
    desired_mcps: list[str],
    previous_skills: list[str],
    previous_mcps: list[str],
    local_skills: dict[str, Any],
    local_mcps: dict[str, Any],
    external_skills: dict[str, dict[str, Any]],
    external_mcps: dict[str, dict[str, Any]],
    catalog_error: str,
) -> dict[str, str]:
    agent_id = str(employee["agentscope_agent_id"])
    active_skill_refs = [
        item
        for item in desired_skills
        if item == TASK_SKILL_ID
        or (
            item in local_skills
            and bool(local_skills[item]["enabled"])
        )
        or (
            item not in local_skills
            and not is_nexus_skill(item)
            and bool(external_skills.get(item, {}).get("enabled", True))
        )
    ]
    active_mcp_refs = [
        item
        for item in desired_mcps
        if (item in local_mcps and bool(local_mcps[item]["enabled"]))
        or (
            item not in local_mcps
            and not is_nexus_mcp(item)
            and bool(external_mcps.get(item, {}).get("enabled", True))
        )
    ]
    # 任务技能由 task_skill.py 单独维护，不在通用同步里重复上传。
    generic_skill_refs = [item for item in active_skill_refs if item != TASK_SKILL_ID]

    installed_skill_items = await client.list_workspace_skills(agent_id, session_id)
    installed_mcp_items = await client.list_workspace_mcp(agent_id, session_id)
    installed_skills = _installed_names(installed_skill_items)
    installed_mcps = _installed_names(installed_mcp_items)

    stale_skill_names: list[str] = []
    stale_mcp_names: list[str] = []
    # 解绑或停用的自建技能/MCP。
    for ref in set(previous_skills) - set(active_skill_refs):
        if ref == TASK_SKILL_ID:
            continue
        if is_nexus_skill(ref):
            row = local_skills.get(ref)
            if row is not None:
                stale_skill_names.extend(_matching_skill_names(str(row["name"]), installed_skills))
        else:
            record = external_skills.get(ref)
            if record is not None:
                stale_skill_names.extend(
                    _matching_skill_names(_skill_record_name(record, ref), installed_skills)
                )
    for ref in set(previous_mcps) - set(active_mcp_refs):
        if is_nexus_mcp(ref):
            row = local_mcps.get(ref)
            if row is not None and str(row["runtime_name"]) in installed_mcps:
                stale_mcp_names.append(str(row["runtime_name"]))
        else:
            record = external_mcps.get(ref)
            if record is not None:
                name = str(record.get("name") or ref)
                if name in installed_mcps:
                    stale_mcp_names.append(name)
    # 绑定仍在但资源被停用：从运行环境收回。
    for ref in desired_skills:
        if ref == TASK_SKILL_ID:
            continue
        if is_nexus_skill(ref):
            row = local_skills.get(ref)
            if row is not None and not bool(row["enabled"]):
                stale_skill_names.extend(_matching_skill_names(str(row["name"]), installed_skills))
        elif not bool(external_skills.get(ref, {}).get("enabled", True)):
            record = external_skills.get(ref)
            if record is not None:
                stale_skill_names.extend(
                    _matching_skill_names(_skill_record_name(record, ref), installed_skills)
                )
    for ref in desired_mcps:
        if is_nexus_mcp(ref):
            row = local_mcps.get(ref)
            if row is not None and not bool(row["enabled"]):
                if str(row["runtime_name"]) in installed_mcps:
                    stale_mcp_names.append(str(row["runtime_name"]))
        elif not bool(external_mcps.get(ref, {}).get("enabled", True)):
            record = external_mcps.get(ref)
            if record is not None:
                name = str(record.get("name") or ref)
                if name in installed_mcps:
                    stale_mcp_names.append(name)

    stale_skill_names = sorted(set(stale_skill_names))
    stale_mcp_names = sorted(set(stale_mcp_names))

    # 需要补装的自建 Skill。
    local_skill_installs: list[tuple[str, Any]] = []
    for ref in generic_skill_refs:
        if is_nexus_skill(ref):
            row = local_skills.get(ref)
            if row is None:
                continue
            name = str(row["name"])
            matches = _matching_skill_names(name, installed_skills)
            row_state = _sync_row_state(connection, "skill", ref, employee["id"], session_id)
            needs_upload = (
                row_state is None
                or row_state["status"] != "applied"
                or int(row_state["revision"] or 0) != int(row["revision"])
            )
            if matches and needs_upload:
                stale_skill_names.extend(matches)
            elif not matches:
                needs_upload = True
            if needs_upload:
                local_skill_installs.append((ref, row))

    # 需要从技能库装入的 AgentScope Skill。
    external_skill_installs: list[str] = []
    for ref in generic_skill_refs:
        if is_nexus_skill(ref) or ref in local_skills:
            continue
        record = external_skills.get(ref)
        if record is None:
            continue
        name = _skill_record_name(record, ref)
        row_state = _sync_row_state(connection, "skill", ref, employee["id"], session_id)
        if row_state is not None and row_state["status"] == "applied" and name in installed_skills:
            continue
        external_skill_installs.append(ref)

    # 需要补装/更新的自建 MCP。
    local_mcp_installs: list[tuple[str, Any]] = []
    for ref in active_mcp_refs:
        if not is_nexus_mcp(ref):
            continue
        row = local_mcps.get(ref)
        if row is None:
            continue
        name = str(row["runtime_name"])
        row_state = _sync_row_state(connection, "mcp", ref, employee["id"], session_id)
        needs_add = (
            row_state is None
            or row_state["status"] != "applied"
            or int(row_state["revision"] or 0) != int(row["revision"])
        )
        if name in installed_mcps and needs_add:
            stale_mcp_names.append(name)
        elif name not in installed_mcps:
            needs_add = True
        if needs_add:
            local_mcp_installs.append((ref, row))

    # 需要从 MCP 库装入的 AgentScope MCP。
    external_mcp_installs: list[str] = []
    for ref in active_mcp_refs:
        if is_nexus_mcp(ref) or ref in local_mcps:
            continue
        record = external_mcps.get(ref)
        if record is None:
            continue
        name = str(record.get("name") or ref)
        row_state = _sync_row_state(connection, "mcp", ref, employee["id"], session_id)
        if row_state is not None and row_state["status"] == "applied" and name in installed_mcps:
            continue
        external_mcp_installs.append(ref)

    mutating = bool(
        stale_skill_names
        or stale_mcp_names
        or local_skill_installs
        or local_mcp_installs
        or external_skill_installs
        or external_mcp_installs
    )
    needs_permission_mode = bool(active_mcp_refs)
    if not mutating and not needs_permission_mode:
        for ref in desired_skills:
            if ref == TASK_SKILL_ID:
                continue
            _set_sync(
                connection,
                "skill",
                ref,
                employee["id"],
                session_id,
                status="applied",
                detail="",
                revision=int(local_skills[ref]["revision"]) if ref in local_skills else 0,
            )
        for ref in desired_mcps:
            _set_sync(
                connection,
                "mcp",
                ref,
                employee["id"],
                session_id,
                status="applied",
                detail="",
                revision=int(local_mcps[ref]["revision"]) if ref in local_mcps else 0,
            )
        connection.commit()
        return {"status": "ok", "detail": ""}

    # 运行中的会话不中途拆改工作区，等下一次发消息或手动重试。
    try:
        state = await client.session_status(agent_id, session_id)
    except AgentScopeError:
        state = "idle"
    if state != "running":
        # 有 MCP 的会话使用 bypass：dont_ask 会把 MCP Tool 的 PASSTHROUGH 权限
        # 转成 DENY，员工就永远调不到 Tool。这是 AgentScope 官方权限模式，不改源码。
        await client.set_permission_mode(
            agent_id,
            session_id,
            "bypass" if active_mcp_refs else UNATTENDED_PERMISSION,
        )
    if mutating and state == "running":
        detail = "会话正在运行，等待下一次同步"
        for ref in desired_skills:
            if ref == TASK_SKILL_ID:
                continue
            _set_sync(
                connection,
                "skill",
                ref,
                employee["id"],
                session_id,
                status="pending",
                detail=detail,
                revision=int(local_skills[ref]["revision"]) if ref in local_skills else 0,
            )
        for ref in desired_mcps:
            _set_sync(
                connection,
                "mcp",
                ref,
                employee["id"],
                session_id,
                status="pending",
                detail=detail,
                revision=int(local_mcps[ref]["revision"]) if ref in local_mcps else 0,
            )
        connection.commit()
        return {"status": "running", "detail": detail}
    if not mutating:
        for ref in desired_skills:
            if ref == TASK_SKILL_ID:
                continue
            _set_sync(
                connection,
                "skill",
                ref,
                employee["id"],
                session_id,
                status="applied",
                detail="",
                revision=int(local_skills[ref]["revision"]) if ref in local_skills else 0,
            )
        for ref in desired_mcps:
            _set_sync(
                connection,
                "mcp",
                ref,
                employee["id"],
                session_id,
                status="applied",
                detail="",
                revision=int(local_mcps[ref]["revision"]) if ref in local_mcps else 0,
            )
        connection.commit()
        return {"status": "ok", "detail": ""}

    failures: list[str] = []
    for name in dict.fromkeys(stale_skill_names):
        try:
            await client.remove_workspace_skill(agent_id, session_id, name)
        except AgentScopeError as exc:
            failures.append(f"收回技能 {name} 失败：{exc.detail}")
    for name in dict.fromkeys(stale_mcp_names):
        try:
            await client.remove_workspace_mcp(agent_id, session_id, name)
        except AgentScopeError as exc:
            failures.append(f"收回 MCP {name} 失败：{exc.detail}")

    for ref, row in local_skill_installs:
        try:
            files = store.read_version_files(ref, int(row["version"]))
            await client.upload_workspace_skill_files(
                agent_id,
                session_id,
                _slug_for_skill(str(row["name"])),
                files,
            )
        except (AgentScopeError, ResourceError) as exc:
            failures.append(f"{row['name']} 未装入：{getattr(exc, 'detail', str(exc))}")

    if external_skill_installs:
        try:
            detail = await client.add_workspace_skills_from_library(
                agent_id,
                session_id,
                external_skill_installs,
            )
            for item in external_skill_installs:
                record = external_skills.get(item, {})
                name = _skill_record_name(record, item)
                if name and name in detail:
                    failures.append(f"{name} 未装入：{detail}")
        except AgentScopeError as exc:
            failures.append(f"技能库技能未装入：{exc.detail}")

    for _ref, row in local_mcp_installs:
        try:
            await client.add_workspace_mcp(
                agent_id,
                session_id,
                name=str(row["runtime_name"]),
                url=str(row["url"]),
                headers=_headers_for_row(row, store),
                timeout=float(row["timeout"] or 30),
            )
        except AgentScopeError as exc:
            failures.append(f"{row['name']} 未连接：{exc.detail}")

    if external_mcp_installs:
        try:
            detail = await client.add_workspace_mcps_from_library(
                agent_id,
                session_id,
                external_mcp_installs,
            )
            if detail:
                failures.append(f"MCP 库资源未装入：{detail}")
        except AgentScopeError as exc:
            failures.append(f"MCP 库资源未装入：{exc.detail}")

    failure_detail = _redact(
        "；".join(dict.fromkeys(failures))[:800],
        _secret_values(local_mcps.values(), store),
    )
    for ref in desired_skills:
        if ref == TASK_SKILL_ID:
            continue
        if is_nexus_skill(ref):
            revision = int(local_skills[ref]["revision"]) if ref in local_skills else 0
            disabled = ref in local_skills and not bool(local_skills[ref]["enabled"])
            if disabled:
                status, detail = "applied", "资源已停用"
            else:
                status = "error" if failure_detail else "applied"
                detail = failure_detail
            _set_sync(
                connection,
                "skill",
                ref,
                employee["id"],
                session_id,
                status=status,
                detail=detail,
                revision=revision,
            )
        elif ref in external_skill_installs:
            _set_sync(
                connection,
                "skill",
                ref,
                employee["id"],
                session_id,
                status="error" if failure_detail else "applied",
                detail=failure_detail,
                revision=0,
            )
    for ref in desired_mcps:
        if is_nexus_mcp(ref):
            revision = int(local_mcps[ref]["revision"]) if ref in local_mcps else 0
            _set_sync(
                connection,
                "mcp",
                ref,
                employee["id"],
                session_id,
                status="error" if failure_detail else "applied",
                detail=failure_detail,
                revision=revision,
            )
        elif ref in external_mcp_installs:
            _set_sync(
                connection,
                "mcp",
                ref,
                employee["id"],
                session_id,
                status="error" if failure_detail else "applied",
                detail=failure_detail,
                revision=0,
            )
    connection.commit()
    if failure_detail:
        return {"status": "error", "detail": failure_detail}
    return {"status": "ok", "detail": ""}


def _sync_row_state(connection, resource_type: str, resource_id: str, employee_id: str, session_id: str):
    return connection.execute(
        """
        SELECT status, detail, revision FROM resource_sync
        WHERE resource_type = ? AND resource_id = ? AND employee_id = ? AND session_id = ?
        """,
        (resource_type, resource_id, employee_id, session_id),
    ).fetchone()


def _secret_values(rows: Iterable[Any], store: ResourceStore) -> list[str]:
    """收集会出现在 MCP 请求里的敏感值，用于错误信息脱敏。"""

    values: list[str] = []
    for row in rows:
        for item in _json_list(row["headers_json"]):
            if isinstance(item, dict) and item.get("value"):
                values.append(str(item["value"]))
        if row["token_ciphertext"]:
            try:
                values.append(store.decrypt_secret(row["token_ciphertext"]))
            except ResourceError:
                pass
    return [item for item in values if item]


def _redact(text: str, secrets: Iterable[str]) -> str:
    cleaned = str(text or "")
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def _headers_for_row(row, store: ResourceStore) -> dict[str, str]:
    headers = {
        str(item.get("key") or "").strip(): str(item.get("value") or "")
        for item in _json_list(row["headers_json"])
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    }
    token = store.decrypt_secret(row["token_ciphertext"]) if row["token_ciphertext"] else ""
    if token:
        if row["auth_type"] == MCP_AUTH_BEARER:
            headers["Authorization"] = f"Bearer {token}"
        elif row["auth_type"] == MCP_AUTH_HEADER:
            headers[str(row["auth_header"] or "Authorization")] = token
    return headers




# ---------------------------------------------------------------------------
# 资源 CRUD
# ---------------------------------------------------------------------------


def _require_skill_name(connection, name: str, exclude: str | None = None) -> None:
    row = connection.execute("SELECT id FROM nexus_skills WHERE name = ?", (name,)).fetchone()
    if row is not None and row["id"] != exclude:
        raise ResourceError(f"技能名称已存在：{name}", 409)


def _require_mcp_name(connection, name: str, exclude: str | None = None) -> None:
    row = connection.execute("SELECT id FROM nexus_mcps WHERE name = ?", (name,)).fetchone()
    if row is not None and row["id"] != exclude:
        raise ResourceError(f"MCP 名称已存在：{name}", 409)


def create_skill(
    connection,
    store: ResourceStore,
    files: dict[str, bytes],
    *,
    enabled: bool = True,
    external_names: set[str] | None = None,
) -> str:
    name, description = validate_skill_files(files)
    _require_skill_name(connection, name)
    if external_names and name in external_names:
        raise ResourceError(f"AgentScope 技能库已有同名技能：{name}", 409)
    skill_id = new_id("nsk")
    version = 1
    now = utcnow()
    store.write_version(skill_id, version, files)
    metadata = file_metadata(files)
    connection.execute(
        """
        INSERT INTO nexus_skills (
            id, name, description, version, revision, runtime_name, files_json, enabled,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
        """,
        (
            skill_id, name, description, version, name,
            json.dumps(metadata, ensure_ascii=False), int(enabled), now, now,
        ),
    )
    connection.execute(
        """
        INSERT INTO nexus_skill_versions (id, skill_id, version, note, files_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (new_id("skv"), skill_id, version, "初始版本", json.dumps(metadata, ensure_ascii=False), now),
    )
    connection.commit()
    return skill_id


def update_skill(
    connection,
    store: ResourceStore,
    skill_id: str,
    *,
    files: dict[str, bytes] | None = None,
    enabled: bool | None = None,
    note: str = "",
    external_names: set[str] | None = None,
) -> bool:
    row = skill_row(connection, skill_id)
    if row is None:
        raise ResourceError("技能不存在", 404)
    changed = False
    now = utcnow()
    if enabled is not None and bool(enabled) != bool(row["enabled"]):
        connection.execute(
            "UPDATE nexus_skills SET enabled = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
            (int(enabled), now, skill_id),
        )
        changed = True
    if files is not None:
        name, description = validate_skill_files(files)
        _require_skill_name(connection, name, skill_id)
        if external_names and name in external_names:
            raise ResourceError(f"AgentScope 技能库已有同名技能：{name}", 409)
        version = int(row["version"]) + 1
        store.write_version(skill_id, version, files)
        metadata = file_metadata(files)
        connection.execute(
            """
            UPDATE nexus_skills
            SET name = ?, description = ?, version = ?, revision = revision + 1,
                runtime_name = ?, files_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name, description, version, name,
                json.dumps(metadata, ensure_ascii=False), now, skill_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO nexus_skill_versions (id, skill_id, version, note, files_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (new_id("skv"), skill_id, version, note, json.dumps(metadata, ensure_ascii=False), now),
        )
        changed = True
    connection.commit()
    return changed


def restore_skill_version(
    connection,
    store: ResourceStore,
    skill_id: str,
    version: int,
    *,
    external_names: set[str] | None = None,
) -> None:
    row = skill_row(connection, skill_id)
    if row is None:
        raise ResourceError("技能不存在", 404)
    source = connection.execute(
        "SELECT 1 FROM nexus_skill_versions WHERE skill_id = ? AND version = ?",
        (skill_id, version),
    ).fetchone()
    if source is None:
        raise ResourceError("技能版本不存在", 404)
    files = store.read_version_files(skill_id, version)
    update_skill(
        connection,
        store,
        skill_id,
        files=files,
        note=f"恢复 v{version}",
        external_names=external_names,
    )


def delete_skill(connection, store: ResourceStore, skill_id: str) -> None:
    row = skill_row(connection, skill_id)
    if row is None:
        raise ResourceError("技能不存在", 404)
    connection.execute(
        "DELETE FROM resource_sync WHERE resource_type = 'skill' AND resource_id = ?",
        (skill_id,),
    )
    connection.execute("DELETE FROM nexus_skill_versions WHERE skill_id = ?", (skill_id,))
    connection.execute("DELETE FROM nexus_skills WHERE id = ?", (skill_id,))
    connection.commit()
    store.delete_skill_files(skill_id)


def skill_versions(connection, skill_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT version, note, files_json, created_at FROM nexus_skill_versions "
        "WHERE skill_id = ? ORDER BY version DESC",
        (skill_id,),
    ).fetchall()
    return [
        {
            "version": int(row["version"]),
            "note": row["note"],
            "files": _json_list(row["files_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def create_mcp(
    connection,
    store: ResourceStore,
    *,
    name: str,
    description: str,
    url: str,
    transport: str,
    headers: Any,
    auth_type: str,
    auth_header: str,
    token: str,
    timeout: float,
    enabled: bool = True,
) -> str:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ResourceError("MCP 名称不能为空")
    if len(clean_name) > 64:
        raise ResourceError("MCP 名称不能超过 64 个字符")
    _require_mcp_name(connection, clean_name)
    clean_url = validate_mcp_url(url)
    clean_headers = normalize_headers(headers)
    allowed_auth = {MCP_AUTH_NONE, MCP_AUTH_BEARER, MCP_AUTH_HEADER}
    clean_auth = auth_type if auth_type in allowed_auth else MCP_AUTH_NONE
    if token and clean_auth == MCP_AUTH_NONE:
        raise ResourceError("填写凭据时必须选择鉴权方式")
    if clean_auth == MCP_AUTH_HEADER and not str(auth_header or "").strip():
        raise ResourceError("自定义请求头鉴权需要填写 Header 名称")
    timeout_value = float(timeout)
    if not 1 <= timeout_value <= 300:
        raise ResourceError("超时时间必须在 1 到 300 秒之间")
    mcp_id = new_id("nmp")
    now = utcnow()
    connection.execute(
        """
        INSERT INTO nexus_mcps (
            id, name, description, url, transport, headers_json, auth_type, auth_header,
            token_ciphertext, timeout, runtime_name, enabled, revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            mcp_id, clean_name, str(description or "").strip(), clean_url,
            MCP_TRANSPORT_HTTP, json.dumps(clean_headers, ensure_ascii=False),
            clean_auth, str(auth_header or "Authorization").strip() or "Authorization",
            store.encrypt_secret(token), timeout_value,
            mcp_runtime_name(clean_name, mcp_id), int(enabled), now, now,
        ),
    )
    connection.commit()
    return mcp_id


def update_mcp(
    connection,
    store: ResourceStore,
    mcp_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    url: str | None = None,
    transport: str | None = None,
    headers: Any = None,
    auth_type: str | None = None,
    auth_header: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
    enabled: bool | None = None,
) -> None:
    row = mcp_row(connection, mcp_id)
    if row is None:
        raise ResourceError("MCP 不存在", 404)
    clean_name = str(name).strip() if name is not None else str(row["name"])
    if not clean_name:
        raise ResourceError("MCP 名称不能为空")
    _require_mcp_name(connection, clean_name, mcp_id)
    clean_url = validate_mcp_url(url) if url is not None else str(row["url"])
    clean_headers = normalize_headers(headers) if headers is not None else _json_list(row["headers_json"])
    clean_auth = auth_type if auth_type is not None else str(row["auth_type"])
    if clean_auth not in {MCP_AUTH_NONE, MCP_AUTH_BEARER, MCP_AUTH_HEADER}:
        clean_auth = MCP_AUTH_NONE
    header_value = auth_header if auth_header is not None else row["auth_header"]
    clean_header = str(header_value).strip() or "Authorization"
    token_ciphertext = row["token_ciphertext"]
    if token is not None:
        token_ciphertext = store.encrypt_secret(token)
    if token_ciphertext and clean_auth == MCP_AUTH_NONE:
        raise ResourceError("已有凭据时必须保留鉴权方式")
    timeout_value = float(timeout if timeout is not None else row["timeout"])
    if not 1 <= timeout_value <= 300:
        raise ResourceError("超时时间必须在 1 到 300 秒之间")
    connection.execute(
        """
        UPDATE nexus_mcps
        SET name = ?, description = ?, url = ?, transport = ?, headers_json = ?,
            auth_type = ?, auth_header = ?, token_ciphertext = ?, timeout = ?,
            enabled = ?, revision = revision + 1, updated_at = ?
        WHERE id = ?
        """,
        (
            clean_name,
            str(description).strip() if description is not None else str(row["description"]),
            clean_url,
            transport if transport in {MCP_TRANSPORT_HTTP} else MCP_TRANSPORT_HTTP,
            json.dumps(clean_headers, ensure_ascii=False),
            clean_auth,
            clean_header,
            token_ciphertext,
            timeout_value,
            int(enabled if enabled is not None else bool(row["enabled"])),
            utcnow(),
            mcp_id,
        ),
    )
    connection.commit()


def delete_mcp(connection, mcp_id: str) -> None:
    row = mcp_row(connection, mcp_id)
    if row is None:
        raise ResourceError("MCP 不存在", 404)
    connection.execute("DELETE FROM resource_sync WHERE resource_type = 'mcp' AND resource_id = ?", (mcp_id,))
    connection.execute("DELETE FROM nexus_mcps WHERE id = ?", (mcp_id,))
    connection.commit()


async def skill_catalog(
    connection,
    client: AgentScopeClient,
    *,
    q: str = "",
    source: str = "",
    status: str = "",
) -> dict[str, Any]:
    employees = all_employees(connection)
    local_rows = connection.execute("SELECT * FROM nexus_skills ORDER BY created_at").fetchall()
    local_names = {str(row["name"]) for row in local_rows}
    items: list[dict[str, Any]] = []
    external_available = False
    external_by_name: set[str] = set()
    if source not in {SOURCE_NEXUS, SOURCE_BUILTIN}:
        try:
            records = await client.list_skill_records()
            external_available = True
            external_by_name = {
                _skill_record_name(item, str(item.get("id") or ""))
                for item in records
            }
            for record in records:
                if _skill_record_name(record, "") in local_names:
                    # 同名自建技能已经由 Nexus 管理，不再重复展示 AgentScope 库记录。
                    continue
                skill_id = str(record.get("id") or record.get("skill_id") or "")
                bound = employees_for(employees, "skills", skill_id)
                sync = _sync_summary(
                    connection,
                    "skill",
                    skill_id,
                    0,
                    bool(record.get("enabled", True)),
                    bound,
                )
                items.append(external_skill_payload(record, bound, sync))
        except AgentScopeError:
            external_available = False
    if source in {"", SOURCE_BUILTIN}:
        bound_builtin = employees_for(employees, "skills", TASK_SKILL_ID)
        items.append(builtin_skill_payload(bound_builtin))
    if source in {"", SOURCE_NEXUS}:
        for row in local_rows:
            bound = employees_for(employees, "skills", row["id"])
            sync = skill_sync_summary(
                connection,
                row["id"],
                int(row["revision"]),
                bool(row["enabled"]),
                bound,
            )
            items.append(skill_payload(row, bound, sync))
    needle = q.strip().lower()
    if needle:
        items = [
            item for item in items
            if needle in f"{item['name']} {item['description']} {item['source']}".lower()
        ]
    if status == "enabled":
        items = [item for item in items if item["enabled"]]
    elif status == "disabled":
        items = [item for item in items if not item["enabled"]]
    items.sort(key=lambda item: (item["source"] != SOURCE_NEXUS, item["name"]))
    return {
        "available": True,
        "agentscope_available": external_available,
        "external_names": sorted(external_by_name),
        "items": items,
    }


async def mcp_catalog(
    connection,
    client: AgentScopeClient,
    *,
    q: str = "",
    source: str = "",
    status: str = "",
) -> dict[str, Any]:
    employees = all_employees(connection)
    local_rows = connection.execute("SELECT * FROM nexus_mcps ORDER BY created_at").fetchall()
    local_runtime_names = {str(row["runtime_name"]) for row in local_rows}
    items: list[dict[str, Any]] = []
    external_available = False
    if source != SOURCE_NEXUS:
        try:
            records = await client.list_mcp_records()
            external_available = True
            for record in records:
                if str(record.get("name") or "") in local_runtime_names:
                    # 自建 MCP 装入 workspace 时 AgentScope 也会登记一条库记录，列表只保留 Nexus 资源。
                    continue
                mcp_id = str(record.get("id") or "")
                bound = employees_for(employees, "mcps", mcp_id)
                sync = _sync_summary(connection, "mcp", mcp_id, 0, bool(record.get("enabled", True)), bound)
                items.append(external_mcp_payload(record, bound, sync))
        except AgentScopeError:
            external_available = False
    if source != SOURCE_AGENTSCOPE:
        for row in local_rows:
            bound = employees_for(employees, "mcps", row["id"])
            sync = mcp_sync_summary(connection, row["id"], int(row["revision"]), bool(row["enabled"]), bound)
            items.append(mcp_payload(row, bound, sync))
    needle = q.strip().lower()
    if needle:
        items = [
            item for item in items
            if needle in f"{item['name']} {item['description']} {item['url']}".lower()
        ]
    if status == "enabled":
        items = [item for item in items if item["enabled"]]
    elif status == "disabled":
        items = [item for item in items if not item["enabled"]]
    items.sort(key=lambda item: (item["source"] != SOURCE_NEXUS, item["name"]))
    return {
        "available": True,
        "agentscope_available": external_available,
        "items": items,
    }


def local_skill_detail(
    connection,
    store: ResourceStore,
    row,
    employees: list[dict[str, Any]],
) -> dict[str, Any]:
    bound = employees_for(employees, "skills", row["id"])
    sync = skill_sync_summary(connection, row["id"], int(row["revision"]), bool(row["enabled"]), bound)
    payload = skill_payload(row, bound, sync)
    files = store.read_version_files(row["id"], int(row["version"]))
    payload["files"] = file_payloads(files)
    payload["versions"] = skill_versions(connection, row["id"])
    return payload


async def external_skill_detail(client: AgentScopeClient, skill_id: str) -> dict[str, Any]:
    record = await client.get_skill(skill_id)
    markdown = str(record.get("markdown") or "")
    return {
        "id": skill_id,
        "name": str(record.get("name") or record.get("display_name") or skill_id),
        "description": str(record.get("description") or ""),
        "source": SOURCE_AGENTSCOPE,
        "version": str(record.get("version") or ""),
        "enabled": bool(record.get("enabled", True)),
        "editable": False,
        "files": [
            {
                "path": "SKILL.md",
                "size": len(markdown.encode("utf-8")),
                "content": markdown,
                "encoding": "utf8",
            }
        ],
        "versions": [],
        "employees": [],
        "sync": {"status": "unbound", "detail": "由 AgentScope 管理", "applied": 0, "pending": 0, "error": 0},
    }


async def ensure_probe_workspace(
    client: AgentScopeClient,
    connection,
    store: ResourceStore,
) -> tuple[str, str]:
    agent_id = store.get_setting(connection, "probe_agent_id")
    session_id = store.get_setting(connection, "probe_session_id")
    if agent_id and session_id:
        try:
            await client.session_status(agent_id, session_id)
            return agent_id, session_id
        except AgentScopeError:
            session_id = ""
    if not agent_id:
        agent_id = await client.create_agent(
            "Nexus 资源校验",
            "只用于校验 Skill/MCP 资源，不参与员工对话。",
            "资源校验",
        )
        store.set_setting(connection, "probe_agent_id", agent_id)
    try:
        session_id = await client.create_probe_session(agent_id, "Nexus 资源校验会话")
    except AgentScopeError:
        agent_id = await client.create_agent(
            "Nexus 资源校验",
            "只用于校验 Skill/MCP 资源，不参与员工对话。",
            "资源校验",
        )
        store.set_setting(connection, "probe_agent_id", agent_id)
        session_id = await client.create_probe_session(agent_id, "Nexus 资源校验会话")
    store.set_setting(connection, "probe_session_id", session_id)
    return agent_id, session_id


async def test_mcp_connection(
    client: AgentScopeClient,
    connection,
    store: ResourceStore,
    *,
    mcp_id: str = "",
    draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """通过 AgentScope 的 workspace 真实连接一次 MCP，返回 Tool 列表或异常。"""

    draft = draft or {}
    started = datetime.now(UTC)
    library_id = ""
    name = ""
    url = ""
    headers: dict[str, str] = {}
    token = ""
    timeout = 30.0
    row = mcp_row(connection, mcp_id) if mcp_id and is_nexus_mcp(mcp_id) else None
    if row is not None:
        name = str(row["runtime_name"])
        url = str(row["url"])
        timeout = float(row["timeout"] or 30)
        headers = {
            str(item.get("key") or "").strip(): str(item.get("value") or "")
            for item in _json_list(row["headers_json"])
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        }
        token = store.decrypt_secret(row["token_ciphertext"]) if row["token_ciphertext"] else ""
        if token:
            if row["auth_type"] == MCP_AUTH_BEARER:
                headers["Authorization"] = f"Bearer {token}"
            elif row["auth_type"] == MCP_AUTH_HEADER:
                headers[str(row["auth_header"] or "Authorization")] = token
    elif mcp_id:
        library_id = mcp_id
        _skills, mcps, _error = await external_catalogs(client, need_skills=False, need_mcps=True)
        record = mcps.get(mcp_id, {})
        name = str(record.get("name") or mcp_id)
        timeout = 30.0
    else:
        name = f"nexus_probe_{uuid4().hex[:8]}"
    if draft:
        if draft.get("name") and row is None and not library_id:
            name = f"nexus_probe_{uuid4().hex[:8]}"
        if draft.get("url"):
            url = validate_mcp_url(str(draft["url"]))
        if draft.get("headers") is not None:
            headers = {
                str(item.get("key") or "").strip(): str(item.get("value") or "")
                for item in normalize_headers(draft.get("headers"))
            }
        if draft.get("token"):
            token = str(draft["token"])
            auth_type = str(draft.get("auth_type") or MCP_AUTH_BEARER)
            if auth_type == MCP_AUTH_BEARER:
                headers["Authorization"] = f"Bearer {token}"
            elif auth_type == MCP_AUTH_HEADER:
                headers[str(draft.get("auth_header") or "Authorization")] = token
        if draft.get("timeout") is not None:
            timeout = float(draft["timeout"])
    if not url and not library_id:
        raise ResourceError("请填写 MCP 地址")
    agent_id, session_id = await ensure_probe_workspace(client, connection, store)
    installed_name = name
    add_failure = ""
    try:
        if library_id:
            detail = await client.add_workspace_mcps_from_library(agent_id, session_id, [library_id])
            if detail:
                add_failure = detail
        else:
            await client.add_workspace_mcp(
                agent_id,
                session_id,
                name=name,
                url=url,
                headers=headers,
                timeout=timeout,
            )
        items = await client.list_workspace_mcp(agent_id, session_id)
        target = next((item for item in items if str(item.get("name") or "") == installed_name), None)
        if target is None and library_id:
            target = next(
                (
                    item
                    for item in items
                    if str(item.get("name") or "")
                    and str(item.get("name") or "") != "nexus-task"
                    and str(item.get("mcp_config", {}).get("url") or "") == ""
                ),
                None,
            )
        if target is None:
            error = _redact(
                add_failure or "AgentScope 没有返回该 MCP 的连接状态",
                [*headers.values(), token],
            )
            return {
                "ok": False,
                "tools": [],
                "error": error,
                "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
            }
        tools = [
            {"name": str(item.get("name") or ""), "description": str(item.get("description") or "")}
            for item in (target.get("tools") or [])
            if isinstance(item, dict)
        ]
        healthy = bool(target.get("is_healthy"))
        error = _redact(str(target.get("error") or add_failure or ""), [*headers.values(), token])
        return {
            "ok": healthy,
            "tools": tools,
            "error": "" if healthy else (error or "MCP 连接失败"),
            "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
        }
    except AgentScopeError as exc:
        return {
            "ok": False,
            "tools": [],
            "error": _redact(exc.detail, [*headers.values(), token]),
            "elapsed_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
        }
    finally:
        try:
            await client.remove_workspace_mcp(agent_id, session_id, installed_name)
        except AgentScopeError:
            pass
        if not library_id and name.startswith("nexus_probe_"):
            try:
                for record in await client.list_mcp_records():
                    if str(record.get("name") or "") == name and record.get("id"):
                        await client.delete_mcp(str(record["id"]))
            except AgentScopeError:
                pass
