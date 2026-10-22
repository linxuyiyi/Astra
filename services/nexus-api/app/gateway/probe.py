"""节点主动探测：网络、服务、模型（真实轻量 chat）与流式 SSE 能力。

NETWORK 执行真实 DNS 解析与 TCP 连接；SERVICE 请求节点可配置的健康路径；
MODEL 发起一次轻量非流式 chat；STREAM 校验 SSE 分块。探测结果只记录耗时、
状态码与错误分类，不记录请求体或凭据。
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from app.gateway.audio import AUDIO_PATH, probe_audio
from app.gateway.models import (
    AUTH_BEARER,
    AUTH_NONE,
    DEFAULT_HEALTH_PATH,
    PROBE_ASR,
    PROBE_EMBEDDING,
    PROBE_MODEL,
    PROBE_NETWORK,
    PROBE_SERVICE,
    PROBE_STREAM,
)

PROBE_CHAT_PATH = "/v1/chat/completions"

# 验证策略：常规一次 SSE，专项一次非流式；SCOPED 只用于诊断，不推进健康授权。
PROBE_STRATEGIES = ("SSE", "NON_STREAM", "EMBEDDING", "ASR", "SCOPED")


def probe_types_for_strategy(strategy: str) -> list[str]:
    if strategy == "ASR":
        return [PROBE_ASR]
    if strategy == "EMBEDDING":
        return [PROBE_EMBEDDING]
    if strategy == "NON_STREAM":
        return [PROBE_MODEL]
    if strategy == "SCOPED":
        return [PROBE_NETWORK, PROBE_SERVICE, PROBE_MODEL, PROBE_STREAM]
    return [PROBE_STREAM]


def strategy_for_types(probe_types: list[str]) -> str:
    types = set(probe_types)
    if types == {PROBE_ASR}:
        return "ASR"
    if types == {PROBE_EMBEDDING}:
        return "EMBEDDING"
    if types == {PROBE_STREAM}:
        return "SSE"
    if types == {PROBE_MODEL}:
        return "NON_STREAM"
    return "SCOPED"


def strategy_for_outcomes(outcomes: list[ProbeOutcome]) -> str:
    return strategy_for_types([outcome.probe_type for outcome in outcomes])


@dataclass
class ProbeOutcome:
    probe_type: str
    success: bool
    latency_ms: int
    status_code: int | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class GatewayProbeClient(Protocol):
    async def probe(
        self,
        node: Any,
        probe_types: list[str],
        api_key: str | None,
    ) -> list[ProbeOutcome]: ...


def build_node_headers(node: Any, api_key: str | None) -> dict[str, str]:
    """按节点认证方式构造下游认证 Header。

    HEADER 方式的自定义 Header 名称取自 ``auth_header``，凭据值只解自加密的
    ``api_key_encrypted``；不存在独立的明文自定义 Header 存储。不记录任何凭据。
    """

    headers: dict[str, str] = {}
    auth_type = str(getattr(node, "auth_type", None) or AUTH_BEARER).upper()
    if api_key and auth_type != AUTH_NONE:
        header_name = str(getattr(node, "auth_header", None) or "Authorization")
        value = f"Bearer {api_key}" if auth_type == AUTH_BEARER else api_key
        headers[header_name] = value
    return headers


def resolve_endpoint(base_url: str) -> tuple[str, int, str]:
    parsed = urlparse(base_url)
    host = parsed.hostname
    if not host:
        raise ValueError("节点地址缺少主机名")
    scheme = parsed.scheme or "http"
    port = parsed.port or (443 if scheme == "https" else 80)
    return host, port, scheme


class HttpGatewayProbeClient:
    def __init__(
        self,
        timeout: float = 5.0,
        *,
        default_health_path: str = DEFAULT_HEALTH_PATH,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._default_health_path = default_health_path or DEFAULT_HEALTH_PATH
        self._transport = transport

    async def probe(
        self,
        node: Any,
        probe_types: list[str],
        api_key: str | None,
    ) -> list[ProbeOutcome]:
        base = node.base_url.rstrip("/")
        headers = build_node_headers(node, api_key)
        outcomes: list[ProbeOutcome] = []
        if PROBE_NETWORK in probe_types:
            start = time.perf_counter()
            outcomes.append(
                await self._bounded(node, PROBE_NETWORK, start, self._probe_network(node.base_url))
            )
        http_types = [item for item in probe_types if item != PROBE_NETWORK]
        if not http_types:
            return outcomes
        async with httpx.AsyncClient(
            timeout=self._timeouts(node), transport=self._transport
        ) as client:
            for probe_type in http_types:
                start = time.perf_counter()
                try:
                    if probe_type == PROBE_ASR:
                        call = self._probe_asr(client, base, headers, node, start)
                    elif probe_type == PROBE_EMBEDDING:
                        call = self._probe_embedding(client, base, headers, node, start)
                    elif probe_type == PROBE_STREAM:
                        call = self._probe_stream(client, base, headers, node, start)
                    elif probe_type == PROBE_MODEL:
                        call = self._probe_model(client, base, headers, node, start)
                    else:
                        call = self._probe_service(client, base, headers, node, start)
                    # 读超时会在每个 keep-alive 分块后重置，所以上限必须是这一次探测的墙钟时间。
                    outcomes.append(await self._bounded(node, probe_type, start, call))
                except httpx.ConnectError as exc:
                    outcomes.append(self._failed(probe_type, start, f"连接失败: {exc}"))
                except httpx.TimeoutException:
                    outcomes.append(self._failed(probe_type, start, "探测超时"))
                except httpx.HTTPError as exc:
                    outcomes.append(self._failed(probe_type, start, f"请求失败: {exc}"))
        return outcomes

    def _deadline(self, node: Any) -> float:
        # 探测有独立超时预算：节点请求超时再大也不能让一次诊断无限占用。
        request = getattr(node, "request_timeout_seconds", None) or self._timeout
        return min(float(request), float(self._timeout))

    async def _bounded(self, node: Any, probe_type: str, start: float, call):
        try:
            return await asyncio.wait_for(call, timeout=self._deadline(node))
        except TimeoutError:
            return self._failed(probe_type, start, "探测超时")

    def _timeouts(self, node: Any) -> httpx.Timeout:
        request = getattr(node, "request_timeout_seconds", None) or self._timeout
        connect = getattr(node, "connect_timeout_seconds", None) or self._timeout
        return httpx.Timeout(request, connect=connect)

    async def _probe_network(self, base_url: str) -> ProbeOutcome:
        start = time.perf_counter()
        try:
            host, port, _scheme = resolve_endpoint(base_url)
        except ValueError as exc:
            return self._failed(PROBE_NETWORK, start, str(exc))
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
            )
        except socket.gaierror as exc:
            return self._failed(PROBE_NETWORK, start, f"DNS 解析失败: {exc}")
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._timeout
            )
        except TimeoutError:
            return self._failed(PROBE_NETWORK, start, "TCP 连接超时")
        except OSError as exc:
            return self._failed(PROBE_NETWORK, start, f"TCP 连接失败: {exc}")
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return ProbeOutcome(
            PROBE_NETWORK,
            True,
            self._elapsed(start),
            detail={"host": host, "port": port},
        )

    async def _probe_service(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str],
        node: Any,
        start: float,
    ) -> ProbeOutcome:
        path = str(getattr(node, "health_path", None) or self._default_health_path)
        if not path.startswith("/"):
            path = "/" + path
        response = await client.get(f"{base}{path}", headers=headers)
        latency = self._elapsed(start)
        success = 200 <= response.status_code < 400
        return ProbeOutcome(
            PROBE_SERVICE,
            success,
            latency,
            status_code=response.status_code,
            error=None if success else f"健康检查返回 {response.status_code}",
            detail={"path": path},
        )

    async def _probe_asr(self, client, base, headers, node, start) -> ProbeOutcome:
        models = list(getattr(node, "models", []) or [])
        if not models:
            return self._failed(PROBE_ASR, start, "节点未配置模型")
        response = await client.post(
            f"{base}{AUDIO_PATH}", headers=headers,
            data={"model": models[0], "response_format": "json"},
            files={"file": ("probe.wav", probe_audio(), "audio/wav")},
        )
        try:
            payload = response.json()
            success = response.status_code == 200 and isinstance(payload.get("text"), str)
        except (ValueError, AttributeError):
            success = False
        return ProbeOutcome(
            PROBE_ASR, success, self._elapsed(start), status_code=response.status_code,
            error=None if success else "语音识别调用失败或未返回 text",
            detail={"model": models[0]},
        )

    async def _probe_embedding(self, client, base, headers, node, start) -> ProbeOutcome:
        models = list(getattr(node, "models", []) or [])
        if not models:
            return self._failed(PROBE_EMBEDDING, start, "节点未配置模型")
        response = await client.post(
            f"{base}/v1/embeddings", headers=headers,
            json={"model": models[0], "input": "ping", "encoding_format": "float"},
        )
        try:
            payload = response.json()
            vector = payload["data"][0]["embedding"]
            success = response.status_code == 200 and isinstance(vector, list) and bool(vector)
        except (ValueError, KeyError, IndexError, TypeError):
            success = False
        return ProbeOutcome(
            PROBE_EMBEDDING, success, self._elapsed(start), status_code=response.status_code,
            error=None if success else "嵌入调用失败或未返回向量", detail={"model": models[0]},
        )

    async def _probe_model(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str],
        node: Any,
        start: float,
    ) -> ProbeOutcome:
        declared = list(getattr(node, "models", []) or [])
        if not declared:
            return ProbeOutcome(PROBE_MODEL, False, self._elapsed(start), error="节点未配置模型")
        model = declared[0]
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
        response = await client.post(f"{base}{PROBE_CHAT_PATH}", headers=headers, json=body)
        latency = self._elapsed(start)
        if response.status_code != 200:
            return ProbeOutcome(
                PROBE_MODEL,
                False,
                latency,
                status_code=response.status_code,
                error=f"轻量调用返回 {response.status_code}",
                detail={"model": model},
            )
        try:
            payload = response.json()
        except ValueError:
            return ProbeOutcome(
                PROBE_MODEL, False, latency, status_code=200, error="响应不是合法 JSON"
            )
        choices = payload.get("choices") if isinstance(payload, dict) else None
        success = bool(choices)
        return ProbeOutcome(
            PROBE_MODEL,
            success,
            latency,
            status_code=response.status_code,
            error=None if success else "响应缺少 choices",
            detail={"model": model, "choices": len(choices) if choices else 0},
        )

    async def _probe_stream(
        self,
        client: httpx.AsyncClient,
        base: str,
        headers: dict[str, str],
        node: Any,
        start: float,
    ) -> ProbeOutcome:
        declared = list(getattr(node, "models", []) or [])
        if not declared:
            return ProbeOutcome(PROBE_STREAM, False, self._elapsed(start), error="节点未配置模型")
        body = {
            "model": declared[0],
            "messages": [{"role": "user", "content": "ping"}],
            "stream": True,
            "max_tokens": 1,
        }
        async with client.stream(
            "POST", f"{base}{PROBE_CHAT_PATH}", headers=headers, json=body
        ) as response:
            if response.status_code != 200:
                return ProbeOutcome(
                    PROBE_STREAM,
                    False,
                    self._elapsed(start),
                    status_code=response.status_code,
                    error=f"流式返回 {response.status_code}",
                )
            chunks = 0
            saw_data = False
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                saw_data = True
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    return ProbeOutcome(
                        PROBE_STREAM,
                        True,
                        self._elapsed(start),
                        status_code=response.status_code,
                        detail={"chunks": chunks, "done": True},
                    )
                if self._is_sse_json(payload):
                    chunks += 1
            success = saw_data and chunks > 0
            return ProbeOutcome(
                PROBE_STREAM,
                success,
                self._elapsed(start),
                status_code=response.status_code,
                error=None if success else "未收到合法 SSE 数据",
                detail={"chunks": chunks, "done": False},
            )

    @staticmethod
    def _is_sse_json(payload: str) -> bool:
        if not payload or payload == "[DONE]":
            return False
        try:
            return isinstance(json.loads(payload), dict)
        except ValueError:
            return False

    @staticmethod
    def _elapsed(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)

    @staticmethod
    def _failed(probe_type: str, start: float, error: str) -> ProbeOutcome:
        return ProbeOutcome(probe_type, False, HttpGatewayProbeClient._elapsed(start), error=error)
