# -*- coding: utf-8 -*-
"""原生运行 guard 的内部授权端点（任务 3.2.2）。

AgentScope 服务的运行 guard 在每次真实运行前回调本端点，由 Nexus 服务端在
**运行前**核实并返回可信授权结果：

* 调用方持有正确的服务端内部秘密（恒定时间比对，绝不下发前端、不写日志）；
* ``agent_id``/``session_id`` 属于一个可信 Conversation 绑定，且 Session
  与 Agent 顶层 id 一致、会话 ``ready``；
* 员工 enabled、``sync_state=ready``、未处于删除中；
* 会话能力快照中的 Knowledge/Skill/MCP 当前仍真实存在，且 Skill/MCP 在
  workspace 中实际装载/健康；返回实际允许的 Skill 名与 MCP 工具名。

目录/能力核对会进行网络 I/O，其间员工可能被禁用；因此**网络核对之后**在短事务
内再读一次 Employee 与 Conversation 状态，禁用/冻结/非 ready 已生效即拒绝。
数据库事务始终不跨网络。任何不满足、未知 Session、目录不可用或内部错误一律
拒绝（fail-closed），不依赖提示词或前端开关。
"""

from __future__ import annotations

import hmac

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from . import resources
from .adapter import AgentScopeHTTPClient, UpstreamError
from .conversations import _find_session, _parse_session_views
from .db import Conversation, Database, Employee
from .employees import prepare_employee_deletion

TOKEN_HEADER = "X-Nexus-Internal-Token"

# 非空 Knowledge 选择必须使用的 static RAG 参数（与共同 factory 写入一致）。
_KNOWLEDGE_PARAMETERS = {
    "mode": "static",
    "top_k": 3,
    "emit_hint_event": True,
}


class AuthorizeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str | None = None
    agent_id: str | None = None
    session_id: str | None = None


class RunFactRequest(BaseModel):
    """只读冻结查询请求（任务 3.2.3.1）。

    Agent Service 的 admission / 冻结与 Session 运行锁在放行前用本端点核对
    真实 Nexus 事实，**绝不**调用完整 ``authorize-run``（那会回查 AgentScope
    目录并可能形成锁/HTTP 环），也**绝不**取得每员工写锁。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    session_id: str | None = None


class PrepareDeleteRequest(BaseModel):
    """内部删除准备请求（任务 3.2.3.1）。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str


def _token_matches(expected: str | None, provided: str | None) -> bool:
    if not expected or provided is None:
        return False
    return hmac.compare_digest(expected, provided)


def _deny(detail: str) -> HTTPException:
    return HTTPException(status_code=403, detail=detail)


async def _load_conversation(
    db: Database,
    session_id: str,
) -> Conversation | None:
    async with db.session() as session:
        return (
            await session.execute(
                select(Conversation).where(
                    Conversation.session_id == session_id,
                ),
            )
        ).scalars().first()


async def _load_employee(db: Database, employee_id: str) -> Employee | None:
    async with db.session() as session:
        return await session.get(Employee, employee_id)


async def _load_employee_by_agent_id(
    db: Database,
    agent_id: str,
) -> Employee | None:
    async with db.session() as session:
        return (
            await session.execute(
                select(Employee).where(Employee.agent_id == agent_id),
            )
        ).scalars().first()


def _employee_runnable(employee: Employee | None, agent_id: str) -> bool:
    return (
        employee is not None
        and bool(employee.enabled)
        and employee.sync_state == "ready"
        and employee.agent_id == agent_id
    )


_KIND_LABEL = {"skills": "Skill", "mcps": "MCP"}


async def _library_name_map(adapter, kind: str) -> dict[str, str]:
    """真实目录中 enabled 且 selectable 的资源 ID → name。

    与资源保存、共同 factory 使用同一 :func:`resources.project_kind` 投影；
    目录不可用明确 fail-closed，忽略 enabled/selectable 会放行已失效能力。
    """
    projected = await resources.project_kind(adapter, kind)
    if not projected["available"]:
        raise _deny(
            f"{_KIND_LABEL.get(kind, kind)} 目录不可用，拒绝运行。",
        )
    result: dict[str, str] = {}
    for item in projected["items"]:
        if item["enabled"] and item.get("selectable", True):
            result[item["id"]] = item["name"]
    return result


async def _resolve_capabilities(
    adapter: AgentScopeHTTPClient,
    conversation: Conversation,
    agent_id: str,
    session_id: str,
) -> tuple[list[str], list[str], list[str]]:
    """核对并返回 ``(allowed_skill_names, allowed_mcp_tool_names, selected_mcps)``。

    任何失效/未装载/不健康都抛 :class:`HTTPException` 403，不返回部分结果。
    ``selected_mcps`` 是本次会话所选 MCP 的**名称**，作为客户端身份传给运行
    guard：工具名可能跨所选冲突，不能靠工具名前缀推断 MCP 身份。
    """
    snapshot = conversation.capability_snapshot or {}

    allowed_skills: list[str] = []
    skill_ids = snapshot.get("skills") or []
    if skill_ids:
        names = await _library_name_map(adapter, "skills")
        installed = await adapter.list_workspace_skills(agent_id, session_id)
        installed_names = {
            entry.get("name")
            for entry in installed
            if isinstance(entry, dict)
        }
        for identifier in skill_ids:
            name = names.get(identifier)
            if not isinstance(name, str) or name not in installed_names:
                raise _deny("会话 Skill 能力已失效或未装载，拒绝运行。")
            allowed_skills.append(name)

    allowed_tools: list[str] = []
    selected_mcps: list[str] = []
    tool_ids = snapshot.get("tools") or []
    if tool_ids:
        names = await _library_name_map(adapter, "mcps")
        status = await adapter.list_workspace_mcps(agent_id, session_id)
        status_by_name = {
            entry.get("name"): entry
            for entry in status
            if isinstance(entry, dict)
        }
        for identifier in tool_ids:
            name = names.get(identifier)
            entry = status_by_name.get(name) if isinstance(name, str) else None
            if entry is None or entry.get("is_healthy") is not True:
                raise _deny("会话 MCP 能力已失效或不健康，拒绝运行。")
            selectable, _ = resources.mcp_support(entry)
            if not selectable:
                # 装配后按真实 MCPClient 属性再次核对：当前版本仅支持无状态
                # Streamable HTTP，其他传输一律拒绝运行。
                raise _deny("会话 MCP 当前版本不支持，拒绝运行。")
            if name not in selected_mcps:
                selected_mcps.append(name)
            tools = entry.get("tools")
            if not isinstance(tools, list):
                raise _deny("会话 MCP 工具列表不可读，拒绝运行。")
            for tool in tools:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    allowed_tools.append(tool["name"])
    return allowed_skills, allowed_tools, selected_mcps


async def _resolve_knowledge(
    adapter: AgentScopeHTTPClient,
    conversation: Conversation,
    agent_id: str,
    session_id: str,
) -> list[str]:
    """核对原生 Session 的 Knowledge 绑定并返回可信所选 KB id。

    不**只**信任本地 ``capability_snapshot``：必须读取真实原生 Session 列表，
    确认 Session 确实绑定到该 Agent，且其
    ``knowledge_config.knowledge_base_ids`` 与可信快照**完全一致**；非空选择还
    须使用约定的 static 参数（``mode=static``/``top_k=3``/``emit_hint_event=true``）。
    任何漂移/缺失/重复/不可读一律 403，绝不把未选 KB 用于检索；目录有效性用与
    资源投影同源的 :func:`resources.project_kind` 核对。
    """
    snapshot = conversation.capability_snapshot or {}
    knowledge_ids = snapshot.get("knowledge") or []
    if (
        not isinstance(knowledge_ids, list)
        or not all(isinstance(item, str) and item for item in knowledge_ids)
        or len(set(knowledge_ids)) != len(knowledge_ids)
    ):
        raise _deny("会话知识库快照不可读，拒绝运行。")

    try:
        views = await adapter.list_sessions(agent_id)
    except UpstreamError:
        raise _deny("无法读取原生会话，拒绝运行。") from None
    sessions = _parse_session_views(views)
    if sessions is None:
        raise _deny("原生会话列表不可读，拒绝运行。")
    session = _find_session(sessions, session_id)
    if session is None or session.get("agent_id") != agent_id:
        raise _deny("会话未绑定到该员工 Agent，拒绝运行。")
    config = session.get("config")
    if not isinstance(config, dict):
        raise _deny("原生会话配置不可读，拒绝运行。")
    # 原生未装配 Knowledge 时 ``knowledge_config`` 为 ``None``，等价于空选择。
    kb_config = config.get("knowledge_config")
    if kb_config is None:
        native_ids: object = []
        params: object = None
    elif isinstance(kb_config, dict):
        native_ids = kb_config.get("knowledge_base_ids", [])
        params = kb_config.get("parameters")
    else:
        raise _deny("原生会话 Knowledge 配置不可读，拒绝运行。")
    if (
        not isinstance(native_ids, list)
        or not all(isinstance(item, str) and item for item in native_ids)
        or len(set(native_ids)) != len(native_ids)
    ):
        raise _deny("原生会话 Knowledge 关联不可读，拒绝运行。")
    if list(native_ids) != list(knowledge_ids):
        raise _deny("原生会话 Knowledge 关联与可信快照不一致，拒绝运行。")
    if knowledge_ids:
        if not isinstance(params, dict):
            raise _deny("原生会话 Knowledge 参数不可读，拒绝运行。")
        for key, expected in _KNOWLEDGE_PARAMETERS.items():
            if params.get(key) != expected:
                raise _deny("原生会话 Knowledge 检索参数已漂移，拒绝运行。")
        try:
            projected = await resources.project_kind(adapter, "knowledge")
        except UpstreamError:
            raise _deny("知识库目录不可用，拒绝运行。") from None
        if not projected["available"]:
            raise _deny("知识库目录不可用，拒绝运行。")
        by_id = {item["id"]: item for item in projected["items"]}
        for identifier in knowledge_ids:
            item = by_id.get(identifier)
            if item is None or not item["enabled"]:
                raise _deny("会话知识库能力已失效，拒绝运行。")
    return list(knowledge_ids)


def register_guard_routes(app: FastAPI) -> None:
    @app.post("/api/internal/authorize-run", tags=["internal"])
    async def authorize_run(
        request: Request,
        payload: AuthorizeRunRequest,
    ) -> dict:
        config = request.app.state.nexus_config
        expected = getattr(config, "internal_token", None)
        provided = request.headers.get(TOKEN_HEADER)
        if not expected:
            # 未配置内部秘密：无法证明调用方可信，一律 fail-closed。
            raise HTTPException(status_code=503, detail="内部授权未配置。")
        if not _token_matches(expected, provided):
            raise HTTPException(status_code=403, detail="内部授权失败。")

        agent_id = payload.agent_id
        session_id = payload.session_id
        if (
            not isinstance(agent_id, str)
            or not agent_id
            or not isinstance(session_id, str)
            or not session_id
        ):
            raise HTTPException(status_code=400, detail="缺少必要标识。")
        expected_user = getattr(config, "internal_user_id", None)
        if (
            payload.user_id is not None
            and expected_user is not None
            and payload.user_id != expected_user
        ):
            raise HTTPException(status_code=403, detail="内部授权失败。")

        db: Database = request.app.state.db
        adapter: AgentScopeHTTPClient = request.app.state.adapter
        conversation = await _load_conversation(db, session_id)
        if conversation is None:
            raise _deny("未知会话，拒绝运行。")
        if conversation.agent_id != agent_id:
            raise _deny("会话与 Agent 绑定不一致，拒绝运行。")
        if conversation.status != "ready":
            raise _deny("会话未就绪，拒绝运行。")
        employee = await _load_employee(db, conversation.employee_id)
        if not _employee_runnable(employee, agent_id):
            raise _deny("员工当前不可运行，拒绝。")

        # Knowledge：核对真实原生 Session 绑定与关联/参数，再核对目录（网络
        # I/O，不持有数据库事务）。
        knowledge_ids = await _resolve_knowledge(
            adapter,
            conversation,
            agent_id,
            session_id,
        )

        allowed_skills, allowed_tools, selected_mcps = await _resolve_capabilities(
            adapter,
            conversation,
            agent_id,
            session_id,
        )

        # 网络核对之后，短事务再读一次：禁用/冻结/非 ready 若已生效必须拒绝。
        conversation_now = await _load_conversation(db, session_id)
        employee_now = (
            await _load_employee(db, conversation_now.employee_id)
            if conversation_now is not None
            else None
        )
        if (
            conversation_now is None
            or conversation_now.status != "ready"
            or conversation_now.agent_id != agent_id
            or not _employee_runnable(employee_now, agent_id)
        ):
            raise _deny("员工或会话状态已变化，拒绝运行。")

        return {
            "allowed": True,
            "conversation_id": conversation_now.id,
            "employee_id": employee_now.id,
            "skills": allowed_skills,
            "tools": allowed_tools,
            "mcps": selected_mcps,
            # 所选 Knowledge 的稳定 id：AgentScope guard 据此核实这些 KB 在
            # 模型调用前都发生真实检索，且不得沿用官方 log-and-skip。
            "knowledge": list(knowledge_ids),
        }

    @app.post("/api/internal/run-fact", tags=["internal"])
    async def run_fact(request: Request, payload: RunFactRequest) -> dict:
        """只读冻结查询：返回真实运行事实，供 Agent Service 放行/冻结判断。

        只做恒定时间秘密比对与**极短数据库读取**：不调用 ``authorize-run``、
        不查 AgentScope 目录、不取每员工写锁、不做网络 I/O，避免与删除准备
        持有的员工锁或 AgentScope 调用形成环。任何未知标识、绑定不一致或
        数据库异常一律 fail-closed（非 200 → 调用方拒绝运行）。
        """
        config = request.app.state.nexus_config
        expected = getattr(config, "internal_token", None)
        if not expected:
            raise HTTPException(status_code=503, detail="内部授权未配置。")
        if not _token_matches(expected, request.headers.get(TOKEN_HEADER)):
            raise HTTPException(status_code=403, detail="内部授权失败。")

        expected_user = getattr(config, "internal_user_id", None)
        provided_user = request.headers.get("X-User-ID")
        if (
            not expected_user
            or provided_user is None
            or provided_user != expected_user
        ):
            raise HTTPException(status_code=403, detail="内部租户不匹配。")

        agent_id = payload.agent_id
        session_id = payload.session_id
        if not agent_id and not session_id:
            raise HTTPException(status_code=400, detail="缺少必要标识。")

        db: Database = request.app.state.db
        conversation: Conversation | None = None
        employee: Employee | None = None
        resolved_agent_id: str | None = None

        if session_id:
            conversation = await _load_conversation(db, session_id)
            if conversation is None:
                raise _deny("未知会话，拒绝运行。")
            if conversation.agent_id is None:
                raise _deny("会话未绑定 Agent，拒绝运行。")
            if agent_id and conversation.agent_id != agent_id:
                raise _deny("会话与 Agent 绑定不一致，拒绝运行。")
            resolved_agent_id = conversation.agent_id
            employee = await _load_employee(db, conversation.employee_id)
        else:
            employee = await _load_employee_by_agent_id(db, agent_id)
            resolved_agent_id = agent_id

        if employee is None or employee.agent_id != resolved_agent_id:
            raise _deny("员工当前不可运行，拒绝。")

        deleting = employee.sync_state == "deleting"
        runnable = (
            employee.enabled
            and employee.sync_state == "ready"
            and employee.agent_id == resolved_agent_id
            and (conversation is None or conversation.status == "ready")
        )
        return {
            "runnable": bool(runnable),
            "deleting": bool(deleting),
            "operation_id": employee.delete_operation_id if deleting else None,
            "agent_id": resolved_agent_id,
            "employee_id": employee.id,
            "session_id": session_id,
        }

    @app.post("/api/internal/prepare-delete", tags=["internal"])
    async def prepare_delete(
        request: Request,
        payload: PrepareDeleteRequest,
    ) -> dict:
        """内部删除准备：短事务落库删除事实，供后续业务 DELETE 复用。

        与只读查询共用内部秘密校验；只做隔离与事实落库，不等待运行退出、
        不做归档、不调用原生 DELETE。结果最小脱敏（员工/Agent/操作 ID）。
        """
        config = request.app.state.nexus_config
        expected = getattr(config, "internal_token", None)
        if not expected:
            raise HTTPException(status_code=503, detail="内部授权未配置。")
        if not _token_matches(expected, request.headers.get(TOKEN_HEADER)):
            raise HTTPException(status_code=403, detail="内部授权失败。")
        expected_user = getattr(config, "internal_user_id", None)
        provided_user = request.headers.get("X-User-ID")
        if (
            not expected_user
            or provided_user is None
            or provided_user != expected_user
        ):
            raise HTTPException(status_code=403, detail="内部租户不匹配。")
        if not payload.employee_id:
            raise HTTPException(status_code=400, detail="缺少员工标识。")
        return await prepare_employee_deletion(request, payload.employee_id)
