# -*- coding: utf-8 -*-
"""Nexus 原生运行 guard 与能力允许目录（OpenSpec 任务 3.2.2）。

每次真实运行前（含后台 wakeup 触发）回调 Nexus 服务端内部授权端点，由服务端
返回**可信授权结果**（员工启停/Conversation 绑定/当前实际能力）。guard：

* 鉴定失败、秘密缺失、回调不可用、响应非法或资源不满足一律 **fail-closed**
  拒绝运行；
* 按授权结果**重建** ``agent.toolkit``：只装配**精确所选**的 Skill 与受控 MCP
  工具的本地 facade，彻底移除默认宿主工具（Bash/Read/Write/…）、
  Task/Team/Schedule 与 ``reset_tools``、未选资源与动态 ``mcps`` 源；空选择
  绝不视为通配；
* 实际安装能力必须**完整覆盖**授权返回的 expected 名称，缺一个即运行前失败，
  绝不因官方 workspace 吞掉失败而退化为裸 LLM 成功；同名工具歧义显式拒绝；
* facade 委托原工具 ``__call__``/``check_permissions``/``check_read_only``；
  ``is_state_injected=True`` 使官方 ``ToolOffloadMiddleware`` 同步执行（长工具
  随原生 run 等待/可取消），并以真实 ``agent.state`` 的 session 身份核对闭包；
  原生 Skill 工具同样套受控 facade；
* ``on_model_call`` 过滤 schema；``on_check_permission`` 只拒绝未选工具，选中的
  交由原生权限链返回（保留 DENY/ASK），不把 ALLOW 改成默认绕过。

运行 guard 的 ``user_id``/``agent_id``/``session_id`` 来自框架装配时的身份闭包
与真实 state，绝不采用模型自报 ID。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncGenerator, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from agentscope.middleware import MiddlewareBase
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import ToolBase, Toolkit

AUTHORIZE_PATH = "/api/internal/authorize-run"
TOKEN_HEADER = "X-Nexus-Internal-Token"

# 框架内部工具名（仅用于识别；是否允许以“真实存在”为准，不做无条件放行）。
SKILL_TOOL_NAME = "Skill"
COMPRESS_TOOL_NAME = "CompressContext"

# 默认禁用的原生工具（仅用于诊断/测试断言，不用于运行时放行判断）。
DISALLOWED_TOOLS = frozenset(
    {
        "Bash",
        "PowerShell",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "TaskCreate",
        "TaskList",
        "TaskGet",
        "TaskUpdate",
        "ToolStop",
        "TeamCreate",
        "TeamSay",
        "TeamDelete",
        "AgentCreate",
        "AgentInvite",
        "ScheduleCreate",
        "ScheduleView",
        "ScheduleDelete",
        "ScheduleList",
        "reset_tools",
    },
)


class RunNotAuthorizedError(RuntimeError):
    """运行 guard 拒绝本次运行；父运行以失败状态呈现。"""


@dataclass(frozen=True)
class RunAuthorization:
    """可信运行授权结果（来自 Nexus 服务端）。"""

    allowed: bool
    reason: str
    skills: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    # 本次可信授权所选的 MCP 名称（客户端身份）；工具名可能跨所选冲突，装配
    # 必须按名字精确筛选客户端，绝不靠工具名前缀推断 MCP 身份。
    mcps: tuple[str, ...] = ()
    # 仅内部诊断使用，不参与放行判断。
    conversation_id: str | None = None
    employee_id: str | None = None


def _strict_str_tuple(payload: dict, key: str) -> tuple[str, ...]:
    """严格解析字符串列表字段；非法结构抛 ``ValueError``（不静默过滤为空）。

    * 字段缺失：返回空元组；
    * 字段存在但不是列表，或含非字符串/空串：抛 ``ValueError``。
    合法的重复项按首次出现去重（不改变合法性判定）。
    """
    if key not in payload:
        return ()
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} 必须是字符串列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{key} 含非法条目")
        if item not in seen:
            seen.add(item)
            result.append(item)
    return tuple(result)


def _parse_authorization(payload: dict) -> RunAuthorization:
    if not isinstance(payload, dict) or payload.get("allowed") is not True:
        return RunAuthorization(allowed=False, reason="authorization_denied")
    try:
        skills = _strict_str_tuple(payload, "skills")
        tools = _strict_str_tuple(payload, "tools")
        mcps = _strict_str_tuple(payload, "mcps")
    except ValueError:
        return RunAuthorization(allowed=False, reason="authorization_invalid")
    # 所选工具必须且只能归属所选 MCP：tools 非空时 mcps 不得为空，否则无法
    # 唯一确定工具所属客户端，一律拒绝。
    if tools and not mcps:
        return RunAuthorization(allowed=False, reason="authorization_invalid")
    return RunAuthorization(
        allowed=True,
        reason="ok",
        skills=skills,
        tools=tools,
        mcps=mcps,
        conversation_id=payload.get("conversation_id"),
        employee_id=payload.get("employee_id"),
    )


class NexusGuardAuthorizer:
    """调用 Nexus 内部授权端点的默认 HTTP 授权器。"""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            trust_env=False,
            timeout=httpx.Timeout(timeout),
        )

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def authorize(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> RunAuthorization:
        try:
            response = await self._client.post(
                AUTHORIZE_PATH,
                json={
                    "user_id": user_id,
                    "agent_id": agent_id,
                    "session_id": session_id,
                },
                headers={TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError:
            return RunAuthorization(
                allowed=False,
                reason="authorization_unavailable",
            )
        if response.status_code != 200:
            return RunAuthorization(
                allowed=False,
                reason="authorization_denied",
            )
        try:
            payload = response.json()
        except ValueError:
            return RunAuthorization(
                allowed=False,
                reason="authorization_invalid",
            )
        return _parse_authorization(payload)


class DenyAllAuthorizer:
    """未配置内部秘密时的 fail-closed 授权器：一切运行明确拒绝。"""

    reason = "guard_unconfigured"

    async def authorize(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> RunAuthorization:
        return RunAuthorization(allowed=False, reason=self.reason)

    async def aclose(self) -> None:  # pragma: no cover - 无资源
        return None


def _state_session_id(state: Any) -> str | None:
    session_id = getattr(state, "session_id", None)
    return session_id if isinstance(session_id, str) else None


def mcp_client_support(client: Any) -> tuple[bool, str | None]:
    """从真实 ``MCPClient`` 属性判断本版本是否支持该 MCP。

    最小支持条件（与后端资源投影同源，服务侧防直接篡改）：只支持无状态
    Streamable HTTP —— ``is_stateful is False``、``mcp_config.type ==
    'http_mcp'``、HTTP(S) URL 且 path 不以 ``/sse`` / ``/messages/`` 结尾。
    返回 ``(selectable, reason)``；``reason`` 为安全中文原因，不含配置/URL/env。
    """
    if getattr(client, "is_stateful", None) is not False:
        return False, "当前版本仅支持无状态 MCP；有状态或 stdio 条目不可选。"
    config = getattr(client, "mcp_config", None)
    if getattr(config, "type", None) != "http_mcp":
        return False, "当前版本仅支持 Streamable HTTP MCP；stdio 等类型不可选。"
    url = getattr(config, "url", None)
    if not isinstance(url, str) or not url:
        return False, "MCP 连接地址缺失，不可选。"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, "MCP 连接地址不是有效 HTTP(S) URL，不可选。"
    path = parts.path
    if path.endswith("/sse") or path.endswith("/messages/"):
        return False, "当前版本不支持 SSE 传输 MCP。"
    return True, None


def _client_supported(client: Any) -> bool:
    """从真实 workspace ``MCPClient`` 属性核对本版本支持条件（防直接篡改）。"""
    return mcp_client_support(client)[0]


class NexusControlledTool(ToolBase):
    """受控工具（MCP 工具或原生 Skill）的本地 facade。

    只保留原工具的公开属性与安全语义，委托原工具执行；以真实 ``agent.state``
    的 session 身份核对闭包 Session。因为 ``is_state_injected=True``、
    ``is_mcp``/``is_external_tool`` 为假，官方 offload 会同步执行它，长工具随
    原生 run 等待/可取消，不转入后台任务。
    """

    is_mcp = False
    is_state_injected = True
    is_external_tool = False

    def __init__(self, original: ToolBase, session_id: str) -> None:
        super().__init__()
        self._original = original
        self._session_id = session_id
        # 原工具需要 state 注入时（如原生 Skill viewer），转发时一并透传。
        self._passthrough_state = bool(
            getattr(original, "is_state_injected", False),
        )
        self.name = original.name
        self.description = original.description
        self.input_schema = original.input_schema
        self.is_read_only = original.is_read_only
        self.is_concurrency_safe = original.is_concurrency_safe
        self.mcp_name = getattr(original, "mcp_name", None)

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: Any,
    ) -> PermissionDecision:
        return await self._original.check_permissions(tool_input, context)

    async def check_read_only(self, tool_input: dict[str, Any]) -> bool:
        return await self._original.check_read_only(tool_input)

    async def call(
        self,
        _agent_state: Any = None,
        **kwargs: Any,
    ) -> Any:
        if _state_session_id(_agent_state) != self._session_id:
            raise RunNotAuthorizedError(
                "受控工具调用的 Session 与运行授权不一致，已拒绝。",
            )
        if self._passthrough_state:
            kwargs["_agent_state"] = _agent_state
        return await self._original(**kwargs)


class RunGuardMiddleware(MiddlewareBase):
    """绑定真实 user/agent/session 的运行 guard 与能力允许目录。"""

    def __init__(
        self,
        authorizer: Any,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace: Any = None,
    ) -> None:
        self._authorizer = authorizer
        self._user_id = user_id
        self._agent_id = agent_id
        self._session_id = session_id
        self._workspace = workspace
        self._authorization: RunAuthorization | None = None
        self._allowed_names: frozenset[str] = frozenset()

    async def _select_skills(self, authorization: RunAuthorization) -> list:
        """仅装配**精确所选** Skill；空选择不装任何 Skill；缺失即 fail-closed。"""
        installed = await self._workspace.list_skills(agent_id=self._agent_id)
        by_name: dict[str, Any] = {}
        for skill in installed:
            name = getattr(skill, "name", None)
            if not isinstance(name, str) or not name:
                continue
            if name in by_name:
                raise RunNotAuthorizedError(
                    f"原生 Skill 名重复，无法唯一装配：{name}",
                )
            by_name[name] = skill
        selected: list = []
        for name in authorization.skills:
            skill = by_name.get(name)
            if skill is None:
                raise RunNotAuthorizedError(
                    f"所选 Skill 未在 workspace 实际装载：{name}",
                )
            selected.append(skill)
        return selected

    async def _collect_mcp_facades(
        self,
        authorization: RunAuthorization,
    ) -> list[ToolBase]:
        """按**所选 MCP 名字**精确筛选客户端，再按 expected 工具名完整收集。

        绝不靠工具名前缀推断 MCP 身份，也绝不枚举未选客户端：工具名可能跨
        客户端冲突（原生合法 ``a`` + 工具 ``b__c`` 与 MCP ``a__b`` + 工具 ``c``
        都得到 ``mcp__a__b__c``），只有可信授权返回的 selected MCP 名称能唯一
        确定客户端身份。所选 MCP 必须存在、唯一且受支持（即使其真实 0 工具也
        仍核对存在/支持）；expected 工具必须被所选客户端完整覆盖且无歧义。
        """
        selected = list(authorization.mcps)
        expected = list(authorization.tools)
        if not selected and not expected:
            return []
        clients = await self._workspace.list_mcps(
            agent_id=self._agent_id,
            session_id=self._session_id,
        )
        by_name: dict[str, list[Any]] = {}
        for client in clients:
            name = getattr(client, "name", None)
            if isinstance(name, str) and name:
                by_name.setdefault(name, []).append(client)

        selected_clients: list[Any] = []
        for name in selected:
            matches = by_name.get(name, [])
            if not matches:
                raise RunNotAuthorizedError(
                    f"所选 MCP 未在 workspace 实际装载：{name}",
                )
            if len(matches) > 1:
                raise RunNotAuthorizedError(
                    f"所选 MCP 名重复，无法唯一装配：{name}",
                )
            client = matches[0]
            if not _client_supported(client):
                raise RunNotAuthorizedError(
                    f"所选 MCP 当前版本不支持：{name}",
                )
            selected_clients.append(client)

        # 只枚举所选客户端；未选客户端绝不被 list_tools 调用。
        tool_by_name: dict[str, ToolBase] = {}
        for client in selected_clients:
            for tool in await client.list_tools():
                name = getattr(tool, "name", None)
                if not isinstance(name, str) or not name:
                    continue
                if name in tool_by_name:
                    raise RunNotAuthorizedError(
                        f"所选 MCP 工具名歧义（存在多个同名工具）：{name}",
                    )
                tool_by_name[name] = tool

        facades: list[ToolBase] = []
        for name in expected:
            tool = tool_by_name.get(name)
            if tool is None:
                raise RunNotAuthorizedError(
                    f"所选 MCP 工具未在所选客户端实际装载：{name}",
                )
            facades.append(NexusControlledTool(tool, self._session_id))
        return facades

    async def _rebuild_toolkit(self, agent: Any) -> None:
        authorization = self._authorization
        if authorization is None or not authorization.allowed:
            raise RunNotAuthorizedError("缺少可信运行授权，拒绝重建工具。")
        if self._workspace is None:
            # 无法读取真实 workspace 能力时 fail-closed，不放行任何工具。
            raise RunNotAuthorizedError("无法解析会话 workspace，拒绝运行。")

        selected_skills = await self._select_skills(authorization)
        mcp_facades = await self._collect_mcp_facades(authorization)

        # 仅在原 toolkit 真实存在压缩工具时保留它；未真实存在的内部工具不虚构。
        internal: list[ToolBase] = []
        existing = getattr(agent, "toolkit", None)
        if existing is not None:
            compress = await existing.get_tool(COMPRESS_TOOL_NAME)
            if compress is not None:
                internal.append(compress)

        # 只装配所选 Skill 与 facade；不传 mcps，不留动态源；未选安装资源不入表。
        toolkit = Toolkit(
            tools=internal + mcp_facades,
            skills_or_loaders=selected_skills,
        )

        allowed: set[str] = {tool.name for tool in mcp_facades}
        if internal:
            allowed.add(COMPRESS_TOOL_NAME)

        # 原生 Skill 工具：真实存在（即确有已选 Skill）时套同一受控 facade，
        # 委托原技能逻辑并绑定闭包 Session；空选择时不存在，不虚构 Skill。
        skill_tool = await toolkit.get_tool(SKILL_TOOL_NAME)
        if skill_tool is not None:
            await toolkit.add_tool(
                NexusControlledTool(skill_tool, self._session_id),
            )
            allowed.add(SKILL_TOOL_NAME)

        agent.toolkit = toolkit
        self._allowed_names = frozenset(allowed)

    async def on_reply(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        try:
            authorization = await self._authorizer.authorize(
                self._user_id,
                self._agent_id,
                self._session_id,
            )
        except Exception:  # noqa: BLE001 - 授权器异常一律 fail-closed
            authorization = RunAuthorization(
                allowed=False,
                reason="authorization_error",
            )
        self._authorization = authorization
        if not authorization.allowed:
            raise RunNotAuthorizedError(
                f"Nexus 运行 guard 拒绝本次运行（{authorization.reason}）。",
            )
        try:
            await self._rebuild_toolkit(agent)
        except RunNotAuthorizedError:
            raise
        except Exception as exc:  # noqa: BLE001 - 重建失败同样 fail-closed
            raise RunNotAuthorizedError(
                f"Nexus 运行 guard 无法构建允许目录（{type(exc).__name__}）。",
            ) from exc
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_model_call(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable],
    ) -> Any:
        tools = input_kwargs.get("tools")
        if isinstance(tools, list):
            filtered = [
                schema
                for schema in tools
                if _schema_tool_name(schema) in self._allowed_names
            ]
            return await next_handler(**{**input_kwargs, "tools": filtered})
        return await next_handler(**input_kwargs)

    async def on_check_permission(
        self,
        agent: Any,
        input_kwargs: dict,
        next_handler: Callable[..., Awaitable],
    ) -> PermissionDecision:
        tool_call = input_kwargs.get("tool_call")
        name = getattr(tool_call, "name", None)
        if not isinstance(name, str) or name not in self._allowed_names:
            # 本层只负责拒绝未选工具；绝不在此放行未授权名称。
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                message="工具不在本次运行的 Nexus 允许目录内。",
            )
        # 选中工具交回原生权限链（含原工具 check_permissions），保留 DENY/ASK。
        return await next_handler(**input_kwargs)


def _schema_tool_name(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    function = schema.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str):
            return name
    name = schema.get("name")
    return name if isinstance(name, str) else None


def make_run_guard_factory(
    authorizer: Any,
) -> Callable[..., Awaitable[list[MiddlewareBase]]]:
    """构造 ``create_app`` 的 ``extra_agent_middlewares`` 工厂（接收 workspace）。"""

    async def factory(
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace: Any = None,
    ) -> list[MiddlewareBase]:
        return [
            RunGuardMiddleware(
                authorizer,
                user_id,
                agent_id,
                session_id,
                workspace,
            ),
        ]

    return factory
