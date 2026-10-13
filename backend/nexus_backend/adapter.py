# -*- coding: utf-8 -*-
"""轻量 AgentScope HTTP 适配器。

后端与联调脚本只通过官方公开 HTTP 路由访问 AgentScope 服务，不导入官方
私有执行模块（``_service`` / ``_manager`` / ``_router``）或复制其源码。

统一约定：

* 每个请求都带上内部租户头 ``X-User-ID``；
* 统一超时与连接超时；
* 任何传输失败或非 2xx 都收敛为 :class:`UpstreamError`，调用方据此显式
  失败，不伪装成功；
* **绝不自动重试**：``POST /chat/`` 这类非幂等请求在超时或连接中断后
  结果未知，重发可能触发第二次推理。调用方必须通过 ``/status`` 与历史
  接口核对，而不是盲目重发。

长回复与断线校准原则：官方瞬时 replay 缓冲最多保留
``SESSION_REPLAY_MAX_LEN``（1000）条事件，assistant 消息在运行
``finally`` 阶段才写入 SQL。因此 SSE 只用于临时展示；收到 ``REPLY_END``
后必须等待 ``/status`` 真正变为 ``idle``，再读取 ``/messages`` 完整历史，
以历史（按 reply_id/message_id 去重与校准）作为最终内容事实源。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

DEFAULT_REQUEST_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 5.0
# SSE 心跳间隔 30s；读超时放宽到该值以上，避免把空闲连接误判为断开。
DEFAULT_STREAM_READ_TIMEOUT = 120.0


def _status_category(status_code: int) -> str:
    """把上游 HTTP 状态归入固定安全中文分类，不带任何响应原文。"""
    if 400 <= status_code < 500:
        return "上游拒绝请求"
    if 500 <= status_code < 600:
        return "上游服务错误"
    return "上游返回非预期状态"


def _transport_detail(exc: httpx.HTTPError) -> str:
    """传输层异常的公开信息：只暴露异常类型，不带消息/URL 原文。"""
    return type(exc).__name__


class UpstreamError(RuntimeError):
    """AgentScope 上游调用失败。

    ``status_code`` 为 ``None`` 表示连接/超时等传输层错误，此时请求是否被
    受理未知，调用方**不得**盲目重发非幂等请求。

    公开信息只由固定安全中文分类、HTTP 状态、method/path 及传输异常类型
    组成；**绝不**携带上游响应原文、SQL 绑定参数或凭据串。
    """

    def __init__(
        self,
        method: str,
        path: str,
        status_code: int | None,
        detail: str,
    ) -> None:
        self.method = method
        self.path = path
        self.status_code = status_code
        self.detail = detail
        super().__init__(self.public_message)

    @property
    def public_message(self) -> str:
        if self.status_code is None:
            return (
                f"无法连接 AgentScope 服务（{self.method} {self.path}）："
                f"{self.detail}"
            )
        return (
            f"AgentScope 服务返回 HTTP {self.status_code}"
            f"（{self.method} {self.path}）：{self.detail}"
        )


@dataclass(frozen=True)
class UpstreamProbe:
    """一次只读探测的结果，不抛异常，供健康检查使用。"""

    reachable: bool
    http_status: int | None
    body: dict = field(default_factory=dict)
    error: str | None = None


class AgentScopeHTTPClient:
    """同步配置、异步调用的 AgentScope HTTP 客户端。"""

    def __init__(
        self,
        base_url: str,
        internal_user_id: str,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        stream_read_timeout: float = DEFAULT_STREAM_READ_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_user_id = internal_user_id
        self._request_timeout = request_timeout
        self._connect_timeout = connect_timeout
        self._stream_read_timeout = stream_read_timeout
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            # 服务仅回环通信：绝不把请求交给系统代理，否则上游停止时会被
            # 代理的 502 伪装成“可达”，健康语义错误。
            trust_env=False,
            timeout=httpx.Timeout(
                request_timeout,
                connect=connect_timeout,
            ),
        )

    @property
    def headers(self) -> dict:
        """所有请求统一携带的内部租户头。"""
        return {"X-User-ID": self._internal_user_id}

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def internal_user_id(self) -> str:
        return self._internal_user_id

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()

    async def __aenter__(self) -> "AgentScopeHTTPClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # 底层请求
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> dict:
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=self.headers,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                method,
                path,
                None,
                _transport_detail(exc),
            ) from exc
        if response.status_code not in expected:
            raise UpstreamError(
                method,
                path,
                response.status_code,
                _status_category(response.status_code),
            )
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise UpstreamError(
                method,
                path,
                response.status_code,
                "响应不是合法 JSON",
            ) from exc

    async def _probe(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
    ) -> UpstreamProbe:
        # 健康探测必须快速失败：上游进程已停止时，复用的 keep-alive 连接
        # 可能半开，使用请求超时会让探测挂起很久。这里把读超时收紧到连接
        # 超时预算内，保证 ``/api/health`` 及时反映不可用。
        probe_timeout = httpx.Timeout(
            self._connect_timeout,
            connect=self._connect_timeout,
        )
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                headers=self.headers,
                timeout=probe_timeout,
            )
        except httpx.HTTPError as exc:
            return UpstreamProbe(
                reachable=False,
                http_status=None,
                error=_transport_detail(exc),
            )
        try:
            body = response.json() if response.content else {}
        except (json.JSONDecodeError, ValueError):
            body = {}
        return UpstreamProbe(
            reachable=True,
            http_status=response.status_code,
            body=body,
            error=(
                None
                if response.is_success
                else _status_category(response.status_code)
            ),
        )

    # ------------------------------------------------------------------
    # 健康与默认配置
    # ------------------------------------------------------------------
    async def native_health(self) -> dict:
        """``GET /health``：官方组件就绪与版本。**不探测模型或数据库**。"""
        return await self._request("GET", "/health")

    async def probe_native_health(self) -> UpstreamProbe:
        return await self._probe("GET", "/health")

    async def extension_health(self) -> dict:
        """``GET /nexus/health``：Nexus 扩展就绪与模型配置状态。"""
        return await self._request("GET", "/nexus/health")

    async def probe_extension_health(self) -> UpstreamProbe:
        return await self._probe("GET", "/nexus/health")

    async def service_defaults(self) -> dict:
        """``GET /nexus/defaults``：默认 ChatModelConfig（不含密钥）。"""
        return await self._request("GET", "/nexus/defaults")

    # ------------------------------------------------------------------
    # 原生 Agent CRUD（始终使用顶层 AgentRecord.id）
    # ------------------------------------------------------------------
    async def create_agent(
        self,
        *,
        name: str,
        system_prompt: str,
        chat_config: dict | None = None,
    ) -> str:
        body: dict[str, Any] = {
            "name": name,
            "system_prompt": system_prompt,
        }
        if chat_config is not None:
            body["chat_config"] = chat_config
        result = await self._request(
            "POST",
            "/agent/",
            json_body=body,
            expected=(201,),
        )
        return result["agent_id"]

    async def list_agents(self) -> dict:
        return await self._request("GET", "/agent/")

    async def update_agent(self, agent_id: str, **fields: Any) -> dict:
        return await self._request(
            "PATCH",
            f"/agent/{agent_id}",
            json_body=fields,
        )

    async def delete_agent(
        self,
        agent_id: str,
        *,
        ignore_missing: bool = False,
    ) -> bool:
        try:
            await self._request(
                "DELETE",
                f"/agent/{agent_id}",
                expected=(204,),
            )
        except UpstreamError as exc:
            if ignore_missing and exc.status_code == 404:
                return False
            raise
        return True

    # ------------------------------------------------------------------
    # 原生 Session
    # ------------------------------------------------------------------
    async def create_session(
        self,
        *,
        agent_id: str,
        name: str | None = None,
        chat_model_config: dict | None = None,
        knowledge_config: dict | None = None,
        workspace_id: str | None = None,
    ) -> str:
        body: dict[str, Any] = {"agent_id": agent_id}
        if name is not None:
            body["name"] = name
        if chat_model_config is not None:
            body["chat_model_config"] = chat_model_config
        if knowledge_config is not None:
            body["knowledge_config"] = knowledge_config
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        result = await self._request(
            "POST",
            "/sessions/",
            json_body=body,
            expected=(201,),
        )
        return result["session_id"]

    async def list_sessions(self, agent_id: str) -> dict:
        return await self._request(
            "GET",
            "/sessions/",
            params={"agent_id": agent_id},
        )

    async def get_session_status(self, agent_id: str, session_id: str) -> dict:
        return await self._request(
            "GET",
            f"/sessions/{session_id}/status",
            params={"agent_id": agent_id},
        )

    async def interrupt_session(self, agent_id: str, session_id: str) -> dict:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/interrupt",
            params={"agent_id": agent_id},
            expected=(202,),
        )

    async def delete_session(
        self,
        agent_id: str,
        session_id: str,
        *,
        ignore_missing: bool = False,
    ) -> bool:
        try:
            await self._request(
                "DELETE",
                f"/sessions/{session_id}",
                params={"agent_id": agent_id},
                expected=(204,),
            )
        except UpstreamError as exc:
            if ignore_missing and exc.status_code == 404:
                return False
            raise
        return True

    async def list_messages(
        self,
        agent_id: str,
        session_id: str,
        *,
        before: str | None = None,
        limit: int = 50,
    ) -> dict:
        """``GET /sessions/{sid}/messages``。

        使用 ``before`` 消息游标分页（``offset`` 已废弃且被忽略）。省略
        ``before`` 时返回最新一页并带 ``event_cursor``，供 SSE 续订使用。
        """
        params: dict[str, Any] = {"agent_id": agent_id, "limit": limit}
        if before is not None:
            params["before"] = before
        return await self._request(
            "GET",
            f"/sessions/{session_id}/messages",
            params=params,
        )

    async def iter_all_messages(
        self,
        agent_id: str,
        session_id: str,
        *,
        page_size: int = 100,
    ) -> list[dict]:
        """用 ``before`` 游标完整翻页读取该会话全部持久化消息。

        严格校验每页契约：``messages`` 必须是消息对象列表且每条含非空
        ``id``，``has_more`` 必须是布尔值；只要 ``has_more`` 为真，本页就
        必须非空且游标必须前进。任何不一致都视为上游契约失败，显式抛出
        :class:`UpstreamError` 说明历史可能不完整，**绝不**用已取到的部分
        页冒充完整历史；跨页重复出现的消息只保留一次。
        """
        path = f"/sessions/{session_id}/messages"

        def invalid(reason: str) -> UpstreamError:
            return UpstreamError(
                "GET",
                path,
                200,
                f"消息分页契约无效，历史可能不完整：{reason}",
            )

        collected: list[dict] = []
        seen_message_ids: set[str] = set()
        seen_cursors: set[str] = set()
        before: str | None = None
        while True:
            page = await self.list_messages(
                agent_id,
                session_id,
                before=before,
                limit=page_size,
            )
            if not isinstance(page, dict):
                raise invalid("响应不是 JSON 对象")
            batch = page.get("messages")
            has_more = page.get("has_more")
            if not isinstance(batch, list):
                raise invalid("messages 不是消息列表")
            if not isinstance(has_more, bool):
                raise invalid("has_more 不是布尔值")
            page_ids: list[str] = []
            for message in batch:
                if not isinstance(message, dict):
                    raise invalid("messages 含非对象消息")
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    raise invalid("消息缺少有效 id")
                page_ids.append(message_id)
            if len(set(page_ids)) != len(page_ids):
                raise invalid("同一页出现重复消息 id")

            # 官方按时间正序返回一页；更早的一页放在前面。只加入本页中尚未
            # 出现过的消息，避免跨页重复消息被重复计入。
            fresh = [
                message
                for message in batch
                if message["id"] not in seen_message_ids
            ]
            collected = fresh + collected
            seen_message_ids.update(page_ids)

            if not has_more:
                return collected

            # has_more 为真：必须有更早的一页，且游标必须向前推进，否则会用
            # 部分结果冒充完整历史或陷入死循环。
            if not batch:
                raise invalid("has_more 为真但本页为空")
            cursor = page_ids[0]
            if cursor in seen_cursors:
                raise invalid("分页游标未推进（重复页）")
            seen_cursors.add(cursor)
            before = cursor

    # ------------------------------------------------------------------
    # Chat 触发与 SSE
    # ------------------------------------------------------------------
    async def trigger_chat(
        self,
        agent_id: str,
        session_id: str,
        input_msg: dict | None,
    ) -> dict:
        """``POST /chat/``（fire-and-forget）。

        返回 ``started`` 只代表已调度，不代表模型成功。**不自动重试**：
        超时后是否已开始推理未知，调用方须用状态与历史核对。
        """
        return await self._request(
            "POST",
            "/chat/",
            json_body={
                "agent_id": agent_id,
                "session_id": session_id,
                "input": input_msg,
            },
        )

    async def stream_events(
        self,
        agent_id: str,
        session_id: str,
        *,
        after: str | None = None,
    ) -> AsyncIterator[dict]:
        """订阅 ``GET /sessions/{sid}/stream`` 并逐条产出解析后的事件。

        调用方消费完后应 ``await stream.aclose()``（或用 ``aclosing``）。
        心跳注释帧与空帧被忽略。
        """
        params: dict[str, Any] = {"agent_id": agent_id}
        if after is not None:
            params["after"] = after
        stream_timeout = httpx.Timeout(
            None,
            connect=self._connect_timeout,
            read=self._stream_read_timeout,
        )
        try:
            request = self._client.build_request(
                "GET",
                f"/sessions/{session_id}/stream",
                params=params,
                headers=self.headers,
                timeout=stream_timeout,
            )
            response = await self._client.send(
                request,
                stream=True,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "GET",
                f"/sessions/{session_id}/stream",
                None,
                _transport_detail(exc),
            ) from exc
        stream_path = f"/sessions/{session_id}/stream"
        try:
            if response.status_code != 200:
                # 绝不把上游错误响应原文放进公开异常。
                raise UpstreamError(
                    "GET",
                    stream_path,
                    response.status_code,
                    _status_category(response.status_code),
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    raise UpstreamError(
                        "GET",
                        stream_path,
                        response.status_code,
                        "事件流包含非法 JSON",
                    ) from None
                if not isinstance(event, dict):
                    raise UpstreamError(
                        "GET",
                        stream_path,
                        response.status_code,
                        "事件流事件不是 JSON 对象",
                    ) from None
                yield event
        except httpx.HTTPError as exc:
            # 读取阶段断流（ReadError/ReadTimeout 等）统一收敛为
            # UpstreamError，不把原始异常 repr 暴露给调用方。
            raise UpstreamError(
                "GET",
                stream_path,
                None,
                _transport_detail(exc),
            ) from None
        finally:
            await response.aclose()
