# -*- coding: utf-8 -*-
"""匿名 owner 的 Conversation 关联与共同原生 Session factory（任务 3.2.2）。

职责边界：

* 只保存 owner、员工关联、唯一原生 Session 绑定、创建状态、创建意图与能力
  快照；原生 Session/消息/AgentState/Memory 仍是执行与内容事实源。
* 每个 Conversation 都通过 **同一 factory** 真实创建原生 Session、绑定
  ``/nexus/defaults`` 模型、按员工默认关联装载 Knowledge config 与
  Skill/MCP from-library，并**再次读回**实际 workspace 能力；模型、稳定
  workspace、Knowledge/Skill/MCP 全部确认前绝不返回 ready。
* 预检（运行 guard 已配置、模型已配置、所选资源当前 enabled/selectable）在
  持久占位**之前**完成；拿到 sessionID 后立刻短事务持久化已知关联，再进行
  能力 I/O，异常/本地保存失败都不丢已知 ID、也不谎称已清理。
* 创建意图在占位落盘时持久化（原生 Agent id、期望模型配置、Knowledge/Skill/
  MCP 稳定 ID、创建时服务代次）；恢复只按该意图与精确 ``session_marker``
  核对原生 Session，绝不从后续可能被编辑的 Employee 默认值重新猜目标。
* ``client_key`` 为必填幂等键：同一 ``owner + client_key`` 的重复请求走同一
  员工锁，重试既可返回既有 ready，也可按精确标记恢复；只有确证服务代次变化
  且标记不存在时才安全释放占位并重新创建。
* 必须校验 ``X-Nexus-Owner`` 为合法 UUID 并据此授权；不信任客户端提交的原生
  Agent/Session id。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .adapter import AgentScopeHTTPClient, UpstreamError
from .db import Conversation, Database, Employee
from .employees import _employee_lock
from .resources import mcp_support, project_kind

OWNER_HEADER = "X-Nexus-Owner"

STATUS_CREATING = "creating"
STATUS_READY = "ready"
STATUS_INCOMPLETE = "incomplete"

TITLE_MAX_LENGTH = 72
# 与 ``Conversation.client_key`` 列宽一致：必填幂等键必须非空且有界。
CLIENT_KEY_MAX_LENGTH = 128

_KIND_LABEL = {"skills": "Skill", "mcps": "MCP", "knowledge": "Knowledge"}
# 员工字段名（skills/tools）→ 官方目录投影键（skills/mcps）。
_CATALOG_KIND = {"skills": "skills", "tools": "mcps", "mcps": "mcps"}


class ConversationCreate(BaseModel):
    """创建 Conversation 请求体（不接受任何原生 id 字段）。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    title: str | None = None
    # 幂等键为必填：保证每次创建都可按 ``owner + client_key`` 重复定位，未知
    # 结果可经同 key 重试恢复，不给"无入口的永久 creating"。
    client_key: str | None = None


class ConversationError(RuntimeError):
    """共同 factory 的明确失败（携带 HTTP 状态与安全中文原因）。"""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.session_id = session_id
        super().__init__(message)


@dataclass(frozen=True)
class _CreateTarget:
    """一次创建的确定目标（占位落盘时持久化为 create_intent）。"""

    agent_id: str
    chat_model_config: dict
    knowledge: tuple[str, ...]
    skills: tuple[str, ...]
    tools: tuple[str, ...]

    def to_intent(self, *, generation_id: str) -> dict:
        return {
            "agent_id": self.agent_id,
            "chat_model_config": dict(self.chat_model_config),
            "knowledge": list(self.knowledge),
            "skills": list(self.skills),
            "tools": list(self.tools),
            "generation_id": generation_id,
        }


def _target_from_intent(intent: object) -> _CreateTarget | None:
    """从持久化创建意图还原确定目标；结构不合法返回 ``None``。"""
    if not isinstance(intent, dict):
        return None
    agent_id = intent.get("agent_id")
    model = intent.get("chat_model_config")
    knowledge = intent.get("knowledge") or []
    skills = intent.get("skills") or []
    tools = intent.get("tools") or []
    if not isinstance(agent_id, str) or not agent_id:
        return None
    if not isinstance(model, dict):
        return None
    if not all(
        isinstance(group, list)
        and all(isinstance(item, str) and item for item in group)
        for group in (knowledge, skills, tools)
    ):
        return None
    return _CreateTarget(
        agent_id=agent_id,
        chat_model_config=model,
        knowledge=tuple(knowledge),
        skills=tuple(skills),
        tools=tuple(tools),
    )


def new_conversation_id() -> str:
    return f"conv_{uuid.uuid4().hex}"


def conversation_marker(conversation_id: str) -> str:
    """精确原生 Session.name 关联标记，未知结果时据此唯一核对。"""
    return f"nexus-conv-{conversation_id}"


def require_owner(request: Request) -> str:
    """校验并要求 ``X-Nexus-Owner`` 为合法 UUID，返回小写规范值。"""
    raw = request.headers.get(OWNER_HEADER)
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=400,
            detail="缺少匿名 owner 标识（X-Nexus-Owner），拒绝访问。",
        )
    try:
        parsed = uuid.UUID(raw.strip())
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="匿名 owner 标识不是合法 UUID。",
        ) from exc
    return str(parsed)


def public_conversation(row: Conversation) -> dict:
    return {
        "id": row.id,
        "owner": row.owner,
        "employee_id": row.employee_id,
        "agent_id": row.agent_id,
        "session_id": row.session_id,
        "title": row.title,
        "status": row.status,
        "capability_snapshot": row.capability_snapshot or {},
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _validate_title(value: str | None) -> str:
    if value is None:
        return "新对话"
    if not isinstance(value, str):
        raise HTTPException(
            status_code=422,
            detail={"errors": {"title": "必须是文本"}},
        )
    text = value.strip()
    if not text:
        return "新对话"
    if len(text) > TITLE_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={"errors": {"title": f"最多 {TITLE_MAX_LENGTH} 个字符"}},
        )
    return text


# ----------------------------------------------------------------------
# 数据库短事务助手（网络 I/O 绝不放在这些事务内）
# ----------------------------------------------------------------------
async def _find_by_client_key(
    db: Database,
    owner: str,
    client_key: str,
) -> Conversation | None:
    async with db.session() as session:
        return (
            await session.execute(
                select(Conversation).where(
                    Conversation.owner == owner,
                    Conversation.client_key == client_key,
                ),
            )
        ).scalars().first()


async def _reload_employee(db: Database, employee_id: str) -> Employee | None:
    async with db.session() as session:
        return await session.get(Employee, employee_id)


async def _reload_conversation(
    db: Database,
    conversation_id: str,
) -> Conversation | None:
    async with db.session() as session:
        return await session.get(Conversation, conversation_id)


async def _set_status(
    db: Database,
    conversation_id: str,
    *,
    status: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    snapshot: dict | None = None,
) -> None:
    async with db.session() as session:
        row = await session.get(Conversation, conversation_id)
        if row is None:
            return
        row.status = status
        if session_id is not None:
            row.session_id = session_id
        if agent_id is not None:
            row.agent_id = agent_id
        if snapshot is not None:
            row.capability_snapshot = snapshot
        if status == STATUS_READY:
            # 转 ready 即已核对原生绑定与能力：瞬时创建意图（含期望模型参数、
            # Knowledge/Skill/MCP 目标、创建代次）不再需要，清理以免残留即成为
            # 第二份事实源。未完成（creating/incomplete）必须保留以供恢复核对。
            row.create_intent = None
        await session.commit()


async def _delete_conversation(db: Database, conversation_id: str) -> None:
    async with db.session() as session:
        row = await session.get(Conversation, conversation_id)
        if row is not None:
            await session.delete(row)
            await session.commit()


def _parse_session_views(views: object) -> list[dict] | None:
    """严格解析 ``GET /sessions/`` 响应；任何畸形返回 ``None``（不冒充空）。

    所有用于“确认不存在 / marker 缺失”判断的 Session 列表解析共用本契约：
    顶层必须是对象且 ``sessions`` 是数组；每项必须是对象且其 ``session`` 为有效
    对象；每个 session 必须含非空字符串 ``id``、非空字符串 ``agent_id`` 与对象
    ``config``（固定官方 OpenAPI 的必需字段）。只对**合法空数组**返回 ``[]``；
    畸形条目绝不被静默跳过并称为空目录。
    """
    if not isinstance(views, dict):
        return None
    sessions = views.get("sessions")
    if not isinstance(sessions, list):
        return None
    result: list[dict] = []
    for entry in sessions:
        if not isinstance(entry, dict):
            return None
        session = entry.get("session")
        if not isinstance(session, dict):
            return None
        identifier = session.get("id")
        agent_id = session.get("agent_id")
        config = session.get("config")
        if not isinstance(identifier, str) or not identifier:
            return None
        if not isinstance(agent_id, str) or not agent_id:
            return None
        if not isinstance(config, dict):
            return None
        result.append(session)
    return result


def _find_session(sessions: list[dict], session_id: str) -> dict | None:
    for session in sessions:
        if session.get("id") == session_id:
            return session
    return None


def _session_name(session: dict) -> str | None:
    config = session.get("config")
    if isinstance(config, dict):
        name = config.get("name")
        return name if isinstance(name, str) else None
    return None


async def _scan_marker(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    marker: str,
) -> tuple[str, str | None]:
    """按精确 ``Session.name`` marker 重新扫描；返回 ``(状态, session_id)``。

    状态取值：``found``（唯一匹配）/ ``absent``（无匹配）/ ``ambiguous``（多个
    匹配）/ ``unavailable``（上游不可读）。用于安全释放前的重新核对，绝不复用
    更早一次扫描的 absence。
    """
    try:
        views = await adapter.list_sessions(agent_id)
    except UpstreamError:
        return "unavailable", None
    sessions = _parse_session_views(views)
    if sessions is None:
        return "unavailable", None
    matches = [
        session for session in sessions if _session_name(session) == marker
    ]
    if not matches:
        return "absent", None
    if len(matches) != 1:
        return "ambiguous", None
    candidate = matches[0].get("id")
    if not isinstance(candidate, str) or not candidate:
        return "unavailable", None
    return "found", candidate


async def _async_read_generation(adapter: AgentScopeHTTPClient) -> str | None:
    """读取服务当前代次；不可读时返回 ``None``（调用方不得继续 POST）。"""
    try:
        health = await adapter.extension_health()
    except UpstreamError:
        return None
    generation = health.get("generation_id") if isinstance(health, dict) else None
    return generation if isinstance(generation, str) and generation else None


async def _library_names(
    adapter: AgentScopeHTTPClient,
    kind: str,
) -> dict[str, str]:
    """真实目录中 enabled 且 selectable 的资源 ID → name。

    与资源保存、运行 guard 使用同一 :func:`resources.project_kind` 投影，确保
    "选择存在且 enabled/selectable" 只有一个事实源；目录不可用明确失败。
    """
    catalog_kind = _CATALOG_KIND.get(kind, kind)
    projected = await project_kind(adapter, catalog_kind)
    if not projected["available"]:
        raise ConversationError(
            503,
            f"{_KIND_LABEL.get(catalog_kind, kind)} 目录不可用："
            f"{projected['reason']}",
        )
    result: dict[str, str] = {}
    for item in projected["items"]:
        if item["enabled"] and item.get("selectable", True):
            result[item["id"]] = item["name"]
    return result


async def _check_catalog(
    adapter: AgentScopeHTTPClient,
    kind: str,
    ids: "list[str] | tuple[str, ...]",
) -> None:
    """用真实目录核对所选资源存在且 enabled/selectable；目录错误明确失败。"""
    if not ids:
        return
    projected = await project_kind(adapter, kind)
    if not projected["available"]:
        raise ConversationError(
            503,
            f"{_KIND_LABEL.get(kind, kind)} 目录不可用：{projected['reason']}",
        )
    by_id = {item["id"]: item for item in projected["items"]}
    for identifier in ids:
        item = by_id.get(identifier)
        if item is None:
            raise ConversationError(
                502,
                f"所选 {_KIND_LABEL.get(kind, kind)} 已不存在：{identifier}",
            )
        if not item["enabled"]:
            raise ConversationError(
                502,
                f"所选 {_KIND_LABEL.get(kind, kind)} 未启用：{identifier}",
            )
        if not item.get("selectable", True):
            raise ConversationError(
                502,
                f"所选 {_KIND_LABEL.get(kind, kind)} 当前版本不支持：{identifier}",
            )


async def _check_target_catalog(
    adapter: AgentScopeHTTPClient,
    target: _CreateTarget,
) -> None:
    """创建/恢复前：用同一真实目录核对全部所选资源。"""
    await _check_catalog(adapter, "knowledge", target.knowledge)
    await _check_catalog(adapter, "skills", target.skills)
    await _check_catalog(adapter, "mcps", target.tools)


# ----------------------------------------------------------------------
# 共同 factory
# ----------------------------------------------------------------------
async def _precheck(
    adapter: AgentScopeHTTPClient,
    employee: Employee,
) -> _CreateTarget:
    """在持久占位前完成、且不调用原生 POST 的预检。"""
    if employee.agent_id is None:
        raise ConversationError(409, "员工尚未绑定原生 Agent。")
    defaults = await adapter.service_defaults()
    if defaults.get("guard_configured") is not True:
        raise ConversationError(
            503,
            "AgentScope 运行 guard 未配置，拒绝创建可运行会话。",
        )
    if defaults.get("model_configured") is not True:
        raise ConversationError(
            503,
            "默认模型未配置，无法创建可用会话；请在服务端配置模型后重试。",
        )
    chat_model_config = defaults.get("chat_model_config")
    if not isinstance(chat_model_config, dict):
        raise ConversationError(502, "默认模型配置响应无效。")
    return _CreateTarget(
        agent_id=employee.agent_id,
        chat_model_config=chat_model_config,
        knowledge=tuple(employee.knowledge or []),
        skills=tuple(employee.skills or []),
        tools=tuple(employee.tools or []),
    )


def _model_matches(native: object, expected: dict) -> bool:
    """核对原生实际 chat_model_config 与预期绑定的身份字段。"""
    if not isinstance(native, dict):
        return False
    for key in ("type", "credential_id", "model"):
        expected_value = expected.get(key)
        native_value = native.get(key)
        if expected_value is None:
            continue
        if native_value != expected_value:
            return False
    # 必须至少确认到具体模型，避免空配置冒充一致。
    return bool(native.get("type")) and bool(native.get("model"))


async def _verify_and_bind(
    adapter: AgentScopeHTTPClient,
    target: _CreateTarget,
    session_id: str,
    marker: str,
) -> dict:
    """核对原生 Session 绑定、模型、workspace 与能力并返回能力快照。

    绑定核对同时校验真实 Session 的 ``agent_id`` 与精确 ``Session.name``
    marker：官方 SQL ``get_session``/``delete_session`` 忽略 ``agent_id``，只有
    名字标记能唯一确认该 Session 属于本 Conversation，绝不误认他人 Session。
    """
    # 官方 SQL get_session 忽略传入 agentID，必须从列表明确定位并核对绑定。
    views = await adapter.list_sessions(target.agent_id)
    sessions = _parse_session_views(views)
    if sessions is None:
        # 列表契约畸形：无法确认绑定或不存在，按上游失败处理（绝不当作缺失）。
        raise ConversationError(
            502,
            "原生 Session 列表响应格式异常，无法核对绑定。",
        )
    session = _find_session(sessions, session_id)
    if session is None or session.get("agent_id") != target.agent_id:
        raise ConversationError(
            502,
            "原生 Session 绑定核对失败：未确认 Session 属于该员工 Agent。",
        )
    if _session_name(session) != marker:
        raise ConversationError(
            502,
            "原生 Session 名称标记与本会话不一致，拒绝绑定。",
        )
    config = session.get("config") or {}
    workspace_id = config.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id:
        # 本项目按 Session 做物理隔离，必须确认原生稳定 workspace 绑定。
        raise ConversationError(
            502,
            "原生 Session 缺少稳定 workspace 绑定，拒绝返回 ready。",
        )
    native_model = config.get("chat_model_config")
    if not _model_matches(native_model, target.chat_model_config):
        raise ConversationError(
            502,
            "原生 Session 的模型绑定与预期不一致。",
        )
    native_kb_config = config.get("knowledge_config") or {}
    native_knowledge = native_kb_config.get("knowledge_base_ids", [])
    if list(native_knowledge) != list(target.knowledge):
        raise ConversationError(
            502,
            "原生 Session 的 Knowledge 关联与所选不一致。",
        )
    if target.knowledge:
        # 非空选择必须落实约定的 static RAG 参数；实际读回不一致即不返回 ready，
        # 避免默认 agentic 不保证首轮检索而被误当已绑定。
        native_params = native_kb_config.get("parameters")
        if not isinstance(native_params, dict) or any(
            native_params.get(key) != value
            for key, value in _KNOWLEDGE_SEARCH_PARAMETERS.items()
        ):
            raise ConversationError(
                502,
                "原生 Session 的 Knowledge 检索参数与约定不一致。",
            )
    await _check_catalog(adapter, "knowledge", target.knowledge)
    await _verify_workspace_capability(
        adapter,
        agent_id=target.agent_id,
        session_id=session_id,
        kind="skills",
        ids=list(target.skills),
    )
    await _verify_workspace_capability(
        adapter,
        agent_id=target.agent_id,
        session_id=session_id,
        kind="tools",
        ids=list(target.tools),
    )
    return {
        "knowledge": list(target.knowledge),
        "skills": list(target.skills),
        "tools": list(target.tools),
    }


async def _verify_workspace_capability(
    adapter: AgentScopeHTTPClient,
    *,
    agent_id: str,
    session_id: str,
    kind: str,
    ids: list[str],
) -> None:
    """从 library 装载后再次读回 workspace，确认所选资源确实生效。"""
    if not ids:
        return
    # 装配前用真实目录核对选择存在且 enabled/selectable。
    await _check_catalog(adapter, _CATALOG_KIND.get(kind, kind), ids)
    add = (
        adapter.add_skills_from_library
        if kind == "skills"
        else adapter.add_mcps_from_library
    )
    result = await add(agent_id, session_id, ids)
    # from-library 结果必须是合法 added/failed；畸形成功不得假 ready。
    if not isinstance(result, dict):
        raise ConversationError(502, f"{kind} 装载响应格式异常。")
    added = result.get("added")
    failed = result.get("failed")
    if not isinstance(added, list) or not isinstance(failed, dict):
        raise ConversationError(502, f"{kind} 装载响应缺少合法 added/failed。")
    if failed:
        raise ConversationError(
            502,
            f"{kind} 能力装载失败：" + "、".join(sorted(str(k) for k in failed)),
        )
    if kind == "skills":
        installed = await adapter.list_workspace_skills(agent_id, session_id)
        installed_by_name = {
            entry.get("name"): entry
            for entry in installed
            if isinstance(entry, dict)
        }
    else:
        installed = await adapter.list_workspace_mcps(agent_id, session_id)
        installed_by_name = {
            entry.get("name"): entry
            for entry in installed
            if isinstance(entry, dict)
        }
    names = await _library_names(adapter, kind)
    expected = {names.get(identifier) for identifier in ids}
    if None in expected:
        raise ConversationError(
            502,
            f"{kind} 能力读回核对失败：所选资源未在真实目录确认。",
        )
    for identifier in ids:
        name = names[identifier]
        entry = installed_by_name.get(name)
        if entry is None:
            raise ConversationError(
                502,
                f"{kind} 能力读回核对失败，未在 workspace 确认所选资源。",
            )
        if kind == "tools":
            if entry.get("is_healthy") is not True:
                raise ConversationError(
                    502,
                    "所选 MCP 未在会话 workspace 健康装载。",
                )
            selectable, reason = mcp_support(entry)
            if not selectable:
                raise ConversationError(
                    502,
                    f"所选 MCP 当前版本不支持：{reason}",
                )


async def _session_owned_by(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    session_id: str,
    marker: str,
) -> str:
    """确认 Session 与本 Conversation 的归属关系。

    返回 ``owned``（属于本 Agent 且名称标记一致）、``absent``（原生合法空列表
    确认不存在）、``foreign``（存在但 Agent 或名称标记不符）、``unknown``
    （上游不可读或列表契约畸形）。畸形列表绝不当作 ``absent``：否则会误删本地
    占位并重建同 key，而真实 Session 仍可能存在。
    """
    try:
        views = await adapter.list_sessions(agent_id)
    except UpstreamError:
        return "unknown"
    sessions = _parse_session_views(views)
    if sessions is None:
        return "unknown"
    session = _find_session(sessions, session_id)
    if session is None:
        return "absent"
    if session.get("agent_id") != agent_id or _session_name(session) != marker:
        return "foreign"
    return "owned"


async def _cleanup_session(
    adapter: AgentScopeHTTPClient,
    agent_id: str,
    session_id: str,
    marker: str,
) -> str:
    """尽力清理**确认属于本 Conversation** 的原生 Session。

    返回 ``confirmed`` / ``unknown`` / ``foreign``。先核对真实归属：

    * 不存在（``absent``）→ ``confirmed``（无须 DELETE，天然收敛，覆盖"DELETE
      实际成功但响应丢失、后续原生已不存在"）；
    * 属于本 Agent 且名称标记一致（``owned``）→ DELETE：官方 204 与 404（已
      不存在）都收敛为 ``confirmed``；
    * 存在但 Agent/名称标记不符（``foreign``）→ 返回 ``foreign`` 且**绝不
      DELETE**（官方 delete 忽略 agent_id，直接删会误删他人 Session）；
    * 其他上游失败 → ``unknown``；不吞任意失败、不重建新 Session。
    """
    ownership = await _session_owned_by(adapter, agent_id, session_id, marker)
    if ownership == "unknown":
        return "unknown"
    if ownership == "absent":
        return "confirmed"
    if ownership == "foreign":
        return "foreign"
    try:
        await adapter.delete_session(agent_id, session_id, ignore_missing=True)
    except UpstreamError:
        return "unknown"
    return "confirmed"


async def _handle_failed_binding(
    db: Database,
    adapter: AgentScopeHTTPClient,
    conversation_id: str,
    agent_id: str,
    *,
    session_id: str,
    marker: str,
) -> tuple[str, Conversation | None]:
    """能力绑定失败：在员工锁内重新读行后再尽力清理自己创建的原生 Session。

    返回 ``(outcome, row)``：

    * ``ready``：锁内重新读行发现已被确认 ready（防御性，不清理、不删除）；
    * ``confirmed``：原生 Session 确认不存在且占位已删除；
    * ``foreign``：原生返回的 Session 不属于本 Conversation，未删除，保留未完成；
    * ``retained``：清理未确认，保留已知关联供同 key 重试核对。
    """
    current = await _reload_conversation(db, conversation_id)
    if current is not None and current.status == STATUS_READY:
        return "ready", current
    outcome = await _cleanup_session(adapter, agent_id, session_id, marker)
    if outcome == "confirmed":
        # 清理前再确认一次状态，绝删除已返回 ready 的 Session。
        current = await _reload_conversation(db, conversation_id)
        if current is not None and current.status == STATUS_READY:
            return "ready", current
        await _delete_conversation(db, conversation_id)
        return "confirmed", None
    if outcome == "foreign":
        await _set_status(db, conversation_id, status=STATUS_INCOMPLETE)
        return "foreign", await _reload_conversation(db, conversation_id)
    await _set_status(db, conversation_id, status=STATUS_INCOMPLETE)
    return "retained", await _reload_conversation(db, conversation_id)


@dataclass(frozen=True)
class _Recovery:
    conversation: Conversation | None
    # recovered / known_absent / marker_absent / unverified / unavailable
    status: str


async def _recover_conversation(
    db: Database,
    adapter: AgentScopeHTTPClient,
    conversation: Conversation,
) -> _Recovery:
    """按持久化创建意图与精确 ``session_marker`` 核对恢复，**不再次 POST**。"""
    target = _target_from_intent(conversation.create_intent)
    if target is None:
        return _Recovery(None, "unavailable")
    try:
        views = await adapter.list_sessions(target.agent_id)
    except UpstreamError:
        return _Recovery(None, "unavailable")
    sessions = _parse_session_views(views)
    if sessions is None:
        # 列表契约畸形：无法确认缺失，保留占位/不 POST（绝不当作 marker 缺失）。
        return _Recovery(None, "unavailable")
    session_id = conversation.session_id
    if session_id is None:
        matches = [
            session
            for session in sessions
            if _session_name(session) == conversation.session_marker
        ]
        if not matches:
            return _Recovery(None, "marker_absent")
        if len(matches) != 1:
            return _Recovery(None, "unavailable")
        candidate = matches[0].get("id")
        if not isinstance(candidate, str) or not candidate:
            return _Recovery(None, "unavailable")
        session_id = candidate
    else:
        session = _find_session(sessions, session_id)
        if session is None:
            # 已知 Session id 且原生已确认不存在：与"未知 POST 的 marker 缺失"
            # 区分 —— POST 已经返回过 id，说明不存在旧的未决创建请求，可同代次
            # 收敛清理，无须重启。
            return _Recovery(None, "known_absent")
    try:
        snapshot = await _verify_and_bind(
            adapter,
            target,
            session_id,
            conversation.session_marker,
        )
    except ConversationError:
        return _Recovery(None, "unverified")
    except UpstreamError:
        return _Recovery(None, "unavailable")
    await _set_status(
        db,
        conversation.id,
        status=STATUS_READY,
        session_id=session_id,
        agent_id=target.agent_id,
        snapshot=snapshot,
    )
    return _Recovery(await _reload_conversation(db, conversation.id), "recovered")


def _ready_existing(
    response: Response,
    existing: Conversation,
    *,
    employee_id: str,
    title: str,
) -> dict:
    if existing.employee_id != employee_id or existing.title != title:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "client_key 已用于其他会话参数，拒绝复用。",
                "conversation_id": existing.id,
            },
        )
    response.status_code = 200
    return public_conversation(existing)


async def _resolve_existing_locked(
    db: Database,
    adapter: AgentScopeHTTPClient,
    response: Response,
    existing: Conversation,
    *,
    employee_id: str,
    title: str,
) -> dict | None:
    """员工锁内处理已存在的幂等键记录。

    返回 ``dict``（已响应）或 ``None``（占位已安全释放，调用方应重新创建）。
    """
    current = await _reload_conversation(db, existing.id)
    if current is None:
        return None
    if current.employee_id != employee_id or current.title != title:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "client_key 已用于其他会话参数，拒绝复用。",
                "conversation_id": current.id,
            },
        )
    if current.status == STATUS_READY:
        response.status_code = 200
        return public_conversation(current)

    recovery = await _recover_conversation(db, adapter, current)
    if recovery.status == "recovered" and recovery.conversation is not None:
        response.status_code = 200
        return public_conversation(recovery.conversation)

    if recovery.status == "known_absent":
        # 已知 Session id 且原生确认不存在（POST 已返回过 id，无未决创建）：
        # 同一代次即可安全收敛，清占位让同 key 重试重新创建，无须重启。
        await _delete_conversation(db, current.id)
        return None

    if recovery.status == "marker_absent":
        target = _target_from_intent(current.create_intent)
        if target is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "会话创建意图不可读，无法安全核对。",
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        intent = (
            current.create_intent
            if isinstance(current.create_intent, dict)
            else {}
        )
        recorded = intent.get("generation_id")
        gen_before = await _async_read_generation(adapter)
        if gen_before is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "message": (
                        "无法读取 AgentScope 服务代次，无法确认旧创建请求是否"
                        "已退出，暂不释放未完成会话。"
                    ),
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        if (
            not isinstance(recorded, str)
            or not recorded
            or gen_before == recorded
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "服务代次未变化，旧创建请求可能仍在进行，不能释放未完成"
                        "会话。请先重启 AgentScope 服务使代次变化后重试。"
                    ),
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        # 代次已确认变化：**重新扫描**精确 marker（绝不复用更早的 absence），
        # 并核对扫描前后代次稳定。竞态窗口内旧请求可能已在本代次落库为既有
        # 记录：重新扫到必须按原意图恢复，不能释放。
        scan, found_sid = await _scan_marker(
            adapter,
            target.agent_id,
            current.session_marker,
        )
        gen_after = await _async_read_generation(adapter)
        if gen_after is None or gen_after != gen_before:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "核对期间服务代次再次变化，无法确认创建结果，保留未完成"
                        "会话，请稍后重试。"
                    ),
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        if scan == "unavailable":
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "无法读取原生会话核对标记，请稍后重试。",
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        if scan == "ambiguous":
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "存在多个同标记原生会话，无法判定，保留未完成状态。",
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        if scan == "found" and found_sid is not None:
            # 重新扫到既有记录：按原意图恢复，不释放占位。
            try:
                snapshot = await _verify_and_bind(
                    adapter,
                    target,
                    found_sid,
                    current.session_marker,
                )
            except ConversationError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": (
                            "重新核对到原生会话但能力未确认，暂不返回 ready。"
                        ),
                        "conversation_id": current.id,
                        "status": current.status,
                    },
                ) from exc
            except UpstreamError as exc:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": "核对原生会话时上游不可用，请稍后重试。",
                        "conversation_id": current.id,
                        "status": current.status,
                    },
                ) from exc
            await _set_status(
                db,
                current.id,
                status=STATUS_READY,
                session_id=found_sid,
                agent_id=target.agent_id,
                snapshot=snapshot,
            )
            recovered_row = await _reload_conversation(db, current.id)
            if recovered_row is not None:
                response.status_code = 200
                return public_conversation(recovered_row)
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "会话记录在恢复后读取失败，请稍后重试。",
                    "conversation_id": current.id,
                    "status": current.status,
                },
            )
        # 重新扫描仍确认不存在且代次稳定：安全释放占位，重新创建。
        await _delete_conversation(db, current.id)
        return None

    if recovery.status == "unverified":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "存在原生 Session 但能力核对未确认，暂不返回 ready。",
                "conversation_id": current.id,
                "status": current.status,
            },
        )
    # unavailable / 其他：目录或上游不可用，无法核对。
    raise HTTPException(
        status_code=503,
        detail={
            "message": "无法核对未完成会话，请稍后重试。",
            "conversation_id": current.id,
            "status": current.status,
        },
    )


def register_conversation_routes(app: FastAPI) -> None:
    @app.post("/api/conversations", status_code=201, tags=["conversations"])
    async def create_conversation(
        request: Request,
        response: Response,
        payload: ConversationCreate,
    ) -> dict:
        owner = require_owner(request)
        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter

        employee_id = payload.employee_id
        if not isinstance(employee_id, str) or not employee_id.strip():
            raise HTTPException(
                status_code=422,
                detail={"errors": {"employee_id": "必填"}},
            )
        employee_id = employee_id.strip()
        title = _validate_title(payload.title)

        if not isinstance(payload.client_key, str) or not payload.client_key.strip():
            raise HTTPException(
                status_code=422,
                detail={"errors": {"client_key": "必填且必须是非空文本"}},
            )
        client_key = payload.client_key.strip()
        if len(client_key) > CLIENT_KEY_MAX_LENGTH:
            raise HTTPException(
                status_code=422,
                detail={
                    "errors": {
                        "client_key": f"最多 {CLIENT_KEY_MAX_LENGTH} 个字符",
                    },
                },
            )

        # 无锁快速路径：只有已确认 ready 的既有记录可直接返回（不会被任何清理
        # 路径删除），其余状态一律进同一员工锁内重新读行处理。
        existing = await _find_by_client_key(db, owner, client_key)
        if existing is not None and existing.status == STATUS_READY:
            return _ready_existing(
                response,
                existing,
                employee_id=employee_id,
                title=title,
            )

        conversation_id = new_conversation_id()
        # 与员工编辑共用同一把员工锁：所有原生创建/恢复路径都在此锁内重新读行，
        # 避免在途创建的能力 I/O 与同 key 重试交错，导致已 ready 的 Session
        # 被原请求的失败清理删除。
        async with _employee_lock(request, employee_id):
            existing = await _find_by_client_key(db, owner, client_key)
            if existing is not None:
                resolved = await _resolve_existing_locked(
                    db,
                    adapter,
                    response,
                    existing,
                    employee_id=employee_id,
                    title=title,
                )
                if resolved is not None:
                    return resolved

            employee = await _reload_employee(db, employee_id)
            if employee is None:
                raise HTTPException(status_code=404, detail="员工不存在。")
            if employee.sync_state != "ready" or employee.agent_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="员工配置未完成一致，暂不能创建会话。",
                )
            if not employee.enabled:
                raise HTTPException(
                    status_code=409,
                    detail="员工已禁用，不能创建新会话。",
                )

            # 预检在持久占位之前完成：未发 POST 时不会留下永久 creating。
            try:
                target = await _precheck(adapter, employee)
                # 创建之前用真实目录核对所选资源存在且 enabled/selectable。
                await _check_target_catalog(adapter, target)
            except ConversationError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "message": exc.message,
                        "employee_id": employee_id,
                    },
                ) from exc

            # 先读取实际服务代次：不可读则不落占位、不 POST。
            generation_id = await _async_read_generation(adapter)
            if generation_id is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "无法读取 AgentScope 服务代次，未创建会话。"
                        "请确认服务已就绪后重试。"
                    ),
                )

            conversation = Conversation(
                id=conversation_id,
                owner=owner,
                employee_id=employee_id,
                agent_id=None,
                session_id=None,
                session_marker=conversation_marker(conversation_id),
                title=title,
                capability_snapshot={},
                create_intent=target.to_intent(generation_id=generation_id),
                client_key=client_key,
                status=STATUS_CREATING,
            )
            try:
                async with db.session() as session:
                    session.add(conversation)
                    await session.commit()
            except IntegrityError as exc:
                existing = await _find_by_client_key(db, owner, client_key)
                if existing is not None:
                    resolved = await _resolve_existing_locked(
                        db,
                        adapter,
                        response,
                        existing,
                        employee_id=employee_id,
                        title=title,
                    )
                    if resolved is not None:
                        return resolved
                raise HTTPException(
                    status_code=500,
                    detail="本地无法保存会话占位，请重试。",
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail="本地无法保存会话占位，请重试。",
                ) from exc

            # 开始原生创建；此后的结果未知必须保留稳定标记，不盲目重 POST。
            try:
                session_id = await adapter.create_session(
                    agent_id=target.agent_id,
                    name=conversation.session_marker,
                    chat_model_config=target.chat_model_config,
                    knowledge_config=_knowledge_config(target),
                    generation_id=generation_id,
                )
            except UpstreamError as exc:
                if _definite_not_received(exc):
                    # 仅明确未受理的 4xx 才清理占位。
                    await _delete_conversation(db, conversation_id)
                    raise HTTPException(
                        status_code=502,
                        detail="原生 Session 创建被拒绝，未保存会话。",
                    ) from exc
                # 异常 2xx / 5xx / 超时：结果未知，保留 creating + 精确标记。
                raise HTTPException(
                    status_code=504 if exc.status_code is None else 502,
                    detail={
                        "message": (
                            "原生 Session 创建结果未确认，已保留未完成会话，"
                            "请用相同 client_key 重试核对恢复。"
                        ),
                        "conversation_id": conversation_id,
                        "status": STATUS_CREATING,
                        "employee_id": employee_id,
                    },
                ) from exc

            # 拿到 sessionID 后立刻短事务持久化已知关联，再进行能力 I/O。
            try:
                await _set_status(
                    db,
                    conversation_id,
                    status=STATUS_CREATING,
                    session_id=session_id,
                    agent_id=target.agent_id,
                )
            except Exception as exc:  # noqa: BLE001
                outcome = await _cleanup_session(
                    adapter,
                    target.agent_id,
                    session_id,
                    conversation.session_marker,
                )
                if outcome != "confirmed":
                    await _set_status(
                        db,
                        conversation_id,
                        status=STATUS_INCOMPLETE,
                        session_id=session_id,
                        agent_id=target.agent_id,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail={
                            "message": (
                                "原生 Session 已创建但本地关联保存失败，且清理"
                                "未确认，会话保留未完成状态。"
                            ),
                            "conversation_id": conversation_id,
                            "session_id": session_id,
                            "status": STATUS_INCOMPLETE,
                        },
                    ) from exc
                await _delete_conversation(db, conversation_id)
                raise HTTPException(
                    status_code=500,
                    detail="本地无法保存会话关联，已清理原生 Session，请重试。",
                ) from exc

            # 能力绑定与读回。
            try:
                snapshot = await _verify_and_bind(
                    adapter,
                    target,
                    session_id,
                    conversation.session_marker,
                )
            except UpstreamError as exc:
                # 结果未知：保留已知 session_id 与精确标记，供同 key 重试恢复。
                raise HTTPException(
                    status_code=504 if exc.status_code is None else 502,
                    detail={
                        "message": (
                            "会话能力核对结果未确认，已保留已知会话关联，"
                            "请用相同 client_key 重试核对恢复。"
                        ),
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "status": STATUS_CREATING,
                        "employee_id": employee_id,
                    },
                ) from exc
            except ConversationError as exc:
                outcome, ready_row = await _handle_failed_binding(
                    db,
                    adapter,
                    conversation_id,
                    target.agent_id,
                    session_id=session_id,
                    marker=conversation.session_marker,
                )
                if outcome == "ready" and ready_row is not None:
                    response.status_code = 200
                    return public_conversation(ready_row)
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "message": (
                            exc.message
                            + _cleanup_suffix(outcome)
                        ),
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "employee_id": employee_id,
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001
                outcome, ready_row = await _handle_failed_binding(
                    db,
                    adapter,
                    conversation_id,
                    target.agent_id,
                    session_id=session_id,
                    marker=conversation.session_marker,
                )
                if outcome == "ready" and ready_row is not None:
                    response.status_code = 200
                    return public_conversation(ready_row)
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": (
                            "会话创建内部错误。"
                            + _cleanup_suffix(outcome)
                        ),
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                    },
                ) from exc

            # 已知关联与能力已核对完成：短事务落 ready。本地落库失败时不清
            # 原生资源、不谎称成功，保留已知 session_id 供后续恢复。
            try:
                await _set_status(
                    db,
                    conversation_id,
                    status=STATUS_READY,
                    session_id=session_id,
                    agent_id=target.agent_id,
                    snapshot=snapshot,
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": (
                            "会话能力已就绪但本地保存失败，已保留已知会话关联，"
                            "请用相同 client_key 重试核对恢复。"
                        ),
                        "conversation_id": conversation_id,
                        "session_id": session_id,
                        "status": STATUS_CREATING,
                        "employee_id": employee_id,
                    },
                ) from exc
            refreshed = await _reload_conversation(db, conversation_id)
            if refreshed is None:
                raise HTTPException(
                    status_code=500,
                    detail="会话记录在绑定后丢失，请重试。",
                )
            return public_conversation(refreshed)


def _cleanup_suffix(outcome: str) -> str:
    """把绑定失败后的清理结果转成安全中文尾注，绝不谎称已清理。"""
    if outcome == "confirmed":
        return "；已清理未完成原生资源。"
    if outcome == "foreign":
        return "；原生返回的 Session 不属于本会话，未删除，保留未完成状态。"
    return "；清理未确认，请用相同 client_key 重试核对。"


# 显式 static RAG 参数：官方默认 agentic 不保证首轮检索；selected KB 的检索
# 失败必须在本轮模型调用前被 guard 发现并中止。
_KNOWLEDGE_SEARCH_PARAMETERS = {
    "mode": "static",
    "top_k": 3,
    "emit_hint_event": True,
}


def _knowledge_config(target: _CreateTarget) -> dict | None:
    if not target.knowledge:
        return None
    return {
        "knowledge_base_ids": list(target.knowledge),
        "parameters": dict(_KNOWLEDGE_SEARCH_PARAMETERS),
    }


def _definite_not_received(exc: UpstreamError) -> bool:
    """仅明确未受理的 4xx 可判定原生未创建。"""
    return (
        exc.status_code is not None and 400 <= exc.status_code < 500
    )
