"""API 中转数据面：独立 ASGI 进程 ``app.gateway.server:app``。

与主 Backend 完全分离：只承载模型数据面转发，不把模型长连接放入任务 Runtime。
启动时加载已保存配置，后台周期刷新，并周期写入实例心跳；
每个请求只读内存配置，运行态并发、周期额度与熔断全部经 Redis 原子执行。

提供模型列表、对话、嵌入与音频转写的 OpenAI 兼容入口。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.database import SessionLocal
from app.gateway.audio import AUDIO_PATH, MAX_AUDIO_BODY_BYTES, parse_audio_upload
from app.gateway.config import (
    ConfigLoader,
    ConfigRefresher,
    GatewayConfig,
    GatewayConfigState,
    KeyConfig,
    NodeConfig,
    PoolConfig,
)
from app.gateway.errors import (
    CLIENT_DISCONNECTED,
    NO_ELIGIBLE_NODE,
    NODE_CREDENTIAL_ERROR,
    POOL_SATURATED,
    QUOTA_EXCEEDED,
    STREAM_EMPTY,
    STREAM_INCOMPLETE,
    STREAM_INTERRUPTED,
    GatewayDataPlaneError,
    invalid_request,
    key_concurrency_exceeded,
    key_disabled,
    key_expired,
    key_forbidden,
    key_quota_exceeded,
    model_not_found,
    no_eligible_node,
    payload_too_large,
    runtime_unavailable,
    unauthorized,
    upstream_protocol_error,
)
from app.gateway.health import (
    acquire_probe_capacity,
    probe_success,
    record_background_probe,
    record_probe_usage,
)
from app.gateway.models import (
    GatewayKey,
    GatewayRequest,
    GatewayRuntimeEvent,
    utcnow,
)
from app.gateway.probe import (
    GatewayProbeClient,
    HttpGatewayProbeClient,
    probe_types_for_strategy,
    strategy_for_types,
)
from app.gateway.proxy import GatewayProxy, NotSentDownstream, build_upstream_headers
from app.gateway.request_lifecycle import recover_abandoned_requests
from app.gateway.runtime import (
    CIRCUIT_HALF_OPEN,
    SCOPE_KEY,
    SCOPE_NODE,
    SCOPE_PROBE,
    RedisRuntimeStore,
    RuntimeStore,
    usage_buckets,
)
from app.gateway.scheduler import (
    REASON_CIRCUIT_OPEN,
    REASON_NODE_FULL,
    REASON_QUOTA_EXCEEDED,
    Candidate,
    effective_weight,
    node_block_reason,
    order_candidates,
    read_node_schedule,
)
from app.gateway.security import CredentialCipher, CredentialError, gateway_key_digest

CHAT_PATH = "/v1/chat/completions"
# 与控制面一致：探测租约 TTL，实例崩溃后自动过期。
PROBE_LEASE_SECONDS = 120
# 节点满载时探测最多等待的秒数；超时只是跳过本轮，不计失败。
PROBE_CAPACITY_WAIT_SECONDS = 5.0
MODELS_PATH = "/v1/models"

# 请求体上限：Content-Length 可提前拒绝，但仍按实际流字节强制，避免声明头被绕过。
MAX_REQUEST_BODY_BYTES = 20 * 1024 * 1024  # 20 MiB

# 明确不转发的响应头：由本进程重新计算长度与连接语义。
# content-encoding 一并去掉：httpx 读到的 body 已经解压，再透传 gzip 会让客户端二次解压失败。
_STRIPPED_RESPONSE_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "trailers",
    "content-encoding",
}

@dataclass
class RequestRecord:
    """调用日志：只保留元数据与测量值，绝不包含 Prompt / Authorization / 响应体。"""

    id: str
    started_at: datetime
    key_id: str | None = None
    owner_id: str | None = None
    model: str | None = None
    actual_model: str | None = None
    pool_id: str | None = None
    node_id: str | None = None
    streaming: bool = False
    retry_count: int = 0
    status_code: int | None = None
    error_code: str | None = None
    stream_interrupted: bool = False
    connect_ms: int | None = None
    ttfb_ms: int | None = None
    ttft_ms: int | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    output_tps: float | None = None
    gateway_instance_id: str | None = None
    gateway_instance_started_at: datetime | None = None
    outcome: str | None = None

    def to_row(self, *, finished: bool) -> GatewayRequest:
        return GatewayRequest(
            id=self.id,
            key_id=self.key_id,
            owner_id=self.owner_id,
            model=self.model,
            actual_model=self.actual_model,
            pool_id=self.pool_id,
            node_id=self.node_id,
            streaming=self.streaming,
            retry_count=self.retry_count,
            status_code=self.status_code,
            error_code=self.error_code,
            stream_interrupted=self.stream_interrupted,
            connect_ms=self.connect_ms,
            ttfb_ms=self.ttfb_ms,
            ttft_ms=self.ttft_ms,
            duration_ms=self.duration_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            total_tokens=self.total_tokens,
            output_tps=self.output_tps,
            started_at=self.started_at,
            finished_at=utcnow() if finished else None,
            gateway_instance_id=self.gateway_instance_id,
            gateway_instance_started_at=self.gateway_instance_started_at,
            outcome=self.outcome,
        )


class StreamUsageCollector:
    """增量解析 SSE ``data:`` 行，提取结尾 usage 与首个非空内容，不缓存完整响应。"""

    def __init__(self) -> None:
        self._pending = bytearray()
        self.usage: dict[str, Any] | None = None
        self.saw_content = False
        # 完整结束：``[DONE]`` 或带非空 finish_reason 的帧。只有这一步能证明流没被截断。
        self.saw_terminal = False

    def feed(self, chunk: bytes) -> bool:
        """喂入一块字节。先攒满一行再按 UTF-8 解码，避免多字节字符被拆开后丢内容。"""

        before = self.saw_content
        self._pending.extend(chunk)
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self._pending[:newline])
            del self._pending[: newline + 1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            self._consume(line.strip())
        return self.saw_content and not before

    def _consume(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        payload = line[len("data:") :].strip()
        if not payload:
            return
        if payload == "[DONE]":
            self.saw_terminal = True
            return
        try:
            body = json.loads(payload)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if isinstance(body.get("usage"), dict):
            self.usage = body["usage"]
        if not self.saw_content and _frame_has_content(body):
            self.saw_content = True
        if not self.saw_terminal and _frame_has_finish(body):
            self.saw_terminal = True


def default_instance_id(configured: str | None) -> str:
    if configured and configured.strip():
        return configured.strip()
    return f"{socket.gethostname()}-{os.getpid()}"


def elapsed_ms(started: datetime) -> int:
    return int((utcnow() - started).total_seconds() * 1000)


def _frame_has_content(body: Mapping[str, Any]) -> bool:
    """首个非空有效内容 token。
    
    扣除 SSE 注释/控制消息（由上层过滤）后，识别首个具有实际生成的帧：
    1. content: str (标准输出正文)
    2. reasoning_content / reasoning: str (深度思考/推理模型 token，如 DeepSeek R1)
    3. tool_calls: list (工具调用帧，含非空名称、参数或 ID)
    空字符串、空参数、role-only 帧不算首 Token。
    """

    choices = body.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            part = choice.get(key)
            if not isinstance(part, dict):
                continue
            # 1. 可见正文 Token
            content = part.get("content")
            if isinstance(content, str) and content:
                return True
            # 2. 推理/深度思考 Token
            reasoning = part.get("reasoning_content") or part.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                return True
            # 3. 工具调用 Token
            tool_calls = part.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    if tc.get("id"):
                        return True
                    fn = tc.get("function")
                    if isinstance(fn, dict) and (fn.get("name") or fn.get("arguments")):
                        return True
    return False


def _frame_has_finish(body: Mapping[str, Any]) -> bool:
    """该帧是否声明生成已结束（OpenAI 兼容的 finish_reason）。"""

    choices = body.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return True
    return False


def _is_event_stream(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "text/event-stream"


class StreamModelRewriter:
    """按行增量改写流式帧的 ``model`` 字段，不缓存整流。

    只处理完整的 ``data:`` 行：JSON 帧替换 ``model``，``[DONE]``、注释与空行原样
    透传。UTF-8 多字节字符被切在块边界时，因为按行重组，不会丢字节。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self._pending = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        self._pending.extend(chunk)
        out = bytearray()
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            raw = bytes(self._pending[: newline + 1])
            del self._pending[: newline + 1]
            out.extend(self._rewrite(raw))
        return bytes(out)

    def flush(self) -> bytes:
        if not self._pending:
            return b""
        raw = bytes(self._pending)
        self._pending.clear()
        return self._rewrite(raw)

    def _rewrite(self, raw: bytes) -> bytes:
        stripped = raw.rstrip(b"\r\n")
        suffix = raw[len(stripped) :]
        if not stripped.startswith(b"data:"):
            return raw
        payload = stripped[len(b"data:") :].strip()
        if not payload or payload == b"[DONE]":
            return raw
        try:
            body = json.loads(payload)
        except ValueError:
            return raw
        if not isinstance(body, dict) or "model" not in body:
            return raw
        body["model"] = self.model
        return b"data: " + json.dumps(body, ensure_ascii=False).encode("utf-8") + suffix


_REQUEST_COLUMNS = (
    "key_id",
    "owner_id",
    "model",
    "actual_model",
    "pool_id",
    "node_id",
    "streaming",
    "retry_count",
    "status_code",
    "error_code",
    "stream_interrupted",
    "connect_ms",
    "ttfb_ms",
    "ttft_ms",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "output_tps",
    "started_at",
    "finished_at",
    "gateway_instance_id",
    "gateway_instance_started_at",
    "outcome",
)


class GatewayDataPlane:
    """数据面运行时：配置、鉴权、调度、转发、日志与心跳。"""

    def __init__(
        self,
        *,
        config_state: GatewayConfigState,
        runtime: RuntimeStore,
        proxy: GatewayProxy,
        cipher: CredentialCipher,
        session_factory: async_sessionmaker[AsyncSession],
        config_loader: ConfigLoader | None = None,
        instance_id: str | None = None,
        heartbeat_interval: float = 10.0,
        heartbeat_ttl: int = 60,
        lease_ttl: int = 900,
        config_poll_interval: float = 10.0,
        runtime_health_interval: float = 5.0,
        timezone: str = "Asia/Shanghai",
        max_retries: int = 1,
        app_version: str = "0.4.0",
        probe_client: GatewayProbeClient | None = None,
        probe_interval: float = 300.0,
        probe_check_interval: float = 10.0,
        circuit_open_seconds: int = 30,
        revocation_sync_interval: float = 60.0,
    ) -> None:
        self.config_state = config_state
        self.runtime = runtime
        self.proxy = proxy
        self.cipher = cipher
        self._session_factory = session_factory
        self._config_loader = config_loader
        self.instance_id = default_instance_id(instance_id)
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_ttl = heartbeat_ttl
        self._lease_ttl = lease_ttl
        self._config_poll_interval = config_poll_interval
        self._runtime_health_interval = runtime_health_interval
        self._timezone = timezone
        self._max_retries = max_retries
        self._app_version = app_version
        self.probe_client = probe_client
        self._probe_interval = max(1.0, float(probe_interval))
        self._probe_check_interval = max(1.0, float(probe_check_interval))
        self._circuit_open_seconds = max(1, int(circuit_open_seconds))
        self._revocation_sync_interval = max(1.0, float(revocation_sync_interval))

        self._runtime_healthy = True
        self._started_at = utcnow()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_probe: dict[str, float] = {}
        self._counters = {"requests": 0, "errors": 0, "active": 0}
        self._last_cpu_sample: tuple[float, float] | None = None

    # ── 生命周期 ───────────────────────────────────────────────────────

    async def startup(self) -> None:
        if self._config_loader is not None:
            try:
                config = await self._config_loader.load_current()
                await self.config_state.replace(config)
            except Exception:  # noqa: BLE001 - 配置加载失败时保持空配置，请求走结构化错误
                pass
        # 吊销阻断集合是数据面鉴权的共享事实来源：启动先从 DB 追加同步并在同一集合写入就绪哨兵。
        # 同步失败（DB 或 Redis 不可用）时不写入哨兵，数据面 fail closed 直到后台重试成功。
        await self.sync_revocation_state()
        # 运行态健康是调度的唯一依据，绝不在启动时用配置健康投影播种：配置健康是
        # 上一次观测的投影，可能已过期，播种会把从未验证的连接伪装成可调度。
        self._runtime_healthy = await self._safe_healthy()
        self._tasks.append(asyncio.create_task(self._runtime_health_loop()))
        self._tasks.append(asyncio.create_task(self._health_probe_loop()))
        self._tasks.append(asyncio.create_task(self._revocation_sync_loop()))
        if self._config_loader is not None:
            refresher = ConfigRefresher(
                state=self.config_state,
                loader=self._config_loader,
                interval=self._config_poll_interval,
            )
            self._tasks.append(asyncio.create_task(refresher.run()))
        # 先发布本代次心跳，再回收上一代表面未完成的请求。心跳失败时不扫描，
        # 避免把自己仍在处理的行误标成结果未知。
        try:
            await self._beat_and_recover()
        except Exception:  # noqa: BLE001 - 启动时运行态不可用不阻断服务
            pass
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await self.runtime.retire(self.instance_id)
        except Exception:  # noqa: BLE001
            pass
        await self.proxy.close()
        await self.runtime.close()

    async def _runtime_health_loop(self) -> None:
        while True:
            self._runtime_healthy = await self._safe_healthy()
            await asyncio.sleep(self._runtime_health_interval)

    async def _safe_healthy(self) -> bool:
        try:
            return await self.runtime.healthy()
        except Exception:  # noqa: BLE001
            return False

    # ── 吊销阻断状态 ───────────────────────────────────────────────────

    async def sync_revocation_state(self) -> bool:
        """从 DB 追加同步所有已吊销 Key 摘要，并把就绪哨兵写入同一 Redis 集合。

        只做追加写入、绝不整表替换，因此不会清空已有或并发写入的吊销。DB 或 Redis
        不可用时返回 False，就绪哨兵保持缺失，数据面据此 fail closed。
        """

        try:
            async with self._session_factory() as session:
                rows = await session.scalars(
                    select(GatewayKey.key_digest).where(GatewayKey.revoked_at.is_not(None))
                )
                digests = [str(digest) for digest in rows if digest]
            await self.runtime.sync_revoked_key_digests(digests)
            return True
        except Exception:  # noqa: BLE001 - 同步失败不阻断服务，数据面 fail closed
            return False

    async def _revocation_sync_loop(self) -> None:
        # Redis 清空或淘汰整个阻断集合键后，哨兵与摘要会一起丢失；
        # 周期从 DB 追平使阻断列表与就绪哨兵重建。
        while True:
            await asyncio.sleep(self._revocation_sync_interval)
            await self.sync_revocation_state()

    async def _check_revocation(self, key: KeyConfig) -> None:
        """在读取请求体与调度之前原子查询共享吊销状态。

        ``is_key_revoked`` 在同一次原子读取里同时检查就绪哨兵与摘要：哨兵缺失或
        Redis 不可达都会抛错，据此 fail closed。绝不先查哨兵再查集合——两次调用之间
        Redis 淘汰整个集合键会形成「哨兵已见、集合已空」的放行窗口。
        """

        try:
            revoked = await self.runtime.is_key_revoked(key.key_digest)
        except GatewayDataPlaneError:
            raise
        except Exception as exc:  # noqa: BLE001 - 运行态不可达或未就绪一律拒绝新请求
            raise runtime_unavailable("吊销阻断状态不可用或尚未就绪，已拒绝新请求") from exc
        if revoked:
            raise key_disabled()

    # ── 节点健康探测 ───────────────────────────────────────────────────

    async def _health_probe_loop(self) -> None:
        if self.probe_client is None:
            return
        while True:
            try:
                await self._probe_due_nodes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 探测循环异常不阻断数据面
                pass
            await asyncio.sleep(self._probe_check_interval)

    async def _probe_due_nodes(self) -> None:
        now = time.monotonic()
        for node in list(self.config_state.config.nodes.values()):
            # 不要求节点已启用或健康：DISABLED/DRAINING/UNKNOWN 也要能做首次验证。
            # 按连接作用域节流：连接变化后新作用域立即重新探测，不等旧作用域间隔。
            scope = node.runtime_scope
            interval = float(node.probe_interval_seconds or self._probe_interval)
            last = self._last_probe.get(scope)
            if last is not None and now - last < interval:
                continue
            self._last_probe[scope] = now
            await self._probe_node(node)

    async def _probe_node(self, node: NodeConfig) -> None:
        """按节点周期执行网络、/health、真实模型与 SSE 探测并写回运行态。"""

        scope = node.runtime_scope
        try:
            api_key = self._decode_node_key(node)
        except GatewayDataPlaneError:
            api_key = None
        # 手动与自动探测共享同一把跨实例租约：任一实例在探测时，其他实例跳过本轮。
        try:
            lease = await self.runtime.acquire(SCOPE_PROBE, node.id, 1, PROBE_LEASE_SECONDS)
        except Exception:  # noqa: BLE001 - 运行态不可用时本轮跳过，不计失败
            return
        if lease is None:
            return
        # 探测共享节点并发容量：满载时短暂等待，仍满则不开始本轮，也不记失败。
        try:
            capacity = await acquire_probe_capacity(
                self.runtime, node, wait_seconds=PROBE_CAPACITY_WAIT_SECONDS
            )
        except Exception:  # noqa: BLE001 - 运行态不可用时本轮跳过
            capacity = None
        if capacity is None:
            await self._safe_release(SCOPE_PROBE, node.id, lease)
            return
        # 常规验证收敛为一次 SSE；策略由节点保存值决定，专项验证为一次非流式。
        selected = probe_types_for_strategy(getattr(node, "probe_strategy", "SSE"))
        try:
            outcomes = await self.probe_client.probe(node, selected, api_key)
        except Exception:  # noqa: BLE001 - 探测本身失败按一次失败计入
            outcomes = []
        finally:
            await self._safe_release(SCOPE_PROBE, node.id, lease)
            await self._safe_release(SCOPE_NODE, node.id, capacity)
        success = probe_success(outcomes)
        detail = {
            "checked": len(outcomes),
            "failed": sum(1 for outcome in outcomes if not outcome.success),
        }
        try:
            await self.runtime.record_probe(
                scope, success=success, detail=detail, at_ms=int(utcnow().timestamp() * 1000)
            )
            # 探测是诊断：失败不打开业务熔断，成功可以关闭已打开的熔断。
            if success:
                await self.runtime.set_circuit_closed(scope)
        except Exception:  # noqa: BLE001 - 运行态写入失败由下一轮探测重试
            pass
        await record_probe_usage(self.runtime, node.id, self._timezone, len(outcomes))
        # 持久化到 gateway_health_checks，并同步节点 DB 健康投影，使管理端与运行态一致。
        # 只有保存连接与本次探测的请求使用的连接一致时才投影，避免旧连接探测判新配置为健康。
        try:
            await record_background_probe(
                self._session_factory,
                node.id,
                outcomes,
                expected_scope=scope,
                strategy=strategy_for_types(selected),
            )
        except Exception:  # noqa: BLE001 - 落库失败不影响数据面，下一轮探测重试
            pass

    def heartbeat_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "app_version": self._app_version,
            "started_at": self._started_at.isoformat(),
            "updated_at": utcnow().isoformat(),
            "uptime_seconds": int((utcnow() - self._started_at).total_seconds()),
            "requests": self._counters["requests"],
            "errors": self._counters["errors"],
            "active": self._counters["active"],
            "heartbeat_ttl": self._heartbeat_ttl,
        }
        # CPU/内存能获得则附带；获取失败时省略字段，管理端按缺省展示。
        payload.update(self._process_metrics())
        return payload

    def _process_metrics(self) -> dict[str, Any]:
        try:
            import resource
        except ImportError:  # pragma: no cover - 非 Unix 平台不提供 resource
            return {}
        metrics: dict[str, Any] = {}
        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            cpu_seconds = float(usage.ru_utime) + float(usage.ru_stime)
            now = time.monotonic()
            if self._last_cpu_sample is not None:
                elapsed = now - self._last_cpu_sample[0]
                if elapsed > 0:
                    delta = cpu_seconds - self._last_cpu_sample[1]
                    metrics["cpu_percent"] = round(max(0.0, delta / elapsed * 100.0), 2)
            self._last_cpu_sample = (now, cpu_seconds)
            rss = int(usage.ru_maxrss)
            # Linux 的 ru_maxrss 单位是 KB，macOS 是字节。
            metrics["memory_bytes"] = rss if sys.platform == "darwin" else rss * 1024
        except Exception:  # noqa: BLE001 - 指标获取失败不阻断心跳
            return metrics
        return metrics

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await self._beat_and_recover()
            except Exception:  # noqa: BLE001 - 心跳失败不阻断服务，也不在本轮扫描
                pass
            await asyncio.sleep(self._heartbeat_interval)

    async def _beat_and_recover(self) -> None:
        await self.runtime.heartbeat(self.instance_id, self.heartbeat_payload(), self._heartbeat_ttl)
        await self._recover_abandoned_requests()

    async def _recover_abandoned_requests(self) -> None:
        async with self._session_factory() as session:
            await recover_abandoned_requests(session, self.runtime)

    async def heartbeat_now(self) -> None:
        try:
            await self.runtime.heartbeat(
                self.instance_id, self.heartbeat_payload(), self._heartbeat_ttl
            )
        except Exception:  # noqa: BLE001
            pass

    @property
    def runtime_healthy(self) -> bool:
        return self._runtime_healthy

    # ── 端点 ───────────────────────────────────────────────────────────

    async def handle_models(self, request: Request) -> JSONResponse:
        config = self.config_state.config
        key = self.authenticate(request.headers.get("authorization"), config=config)
        await self._check_revocation(key)
        models: list[str] = []
        for model, route in config.routes.items():
            if not route.enabled:
                continue
            pool = config.pools.get(route.pool_id)
            if pool is None or not pool.enabled:
                continue
            if key.allowed_models and model not in key.allowed_models:
                continue
            models.append(model)
        now = int(time.time())
        data = [
            {"id": model, "object": "model", "created": now, "owned_by": "nexus-gateway"}
            for model in sorted(models)
        ]
        return JSONResponse({"object": "list", "data": data})

    async def handle_chat(self, request: Request) -> Response:
        return await self._handle_model_request(request, path=CHAT_PATH)

    async def handle_embeddings(self, request: Request) -> Response:
        return await self._handle_model_request(request, path="/v1/embeddings")

    async def handle_transcriptions(self, request: Request) -> Response:
        return await self._handle_model_request(request, path=AUDIO_PATH)

    async def _handle_model_request(self, request: Request, *, path: str) -> Response:
        config = self.config_state.config
        started = utcnow()
        record = RequestRecord(
            id=uuid4().hex,
            started_at=started,
            gateway_instance_id=self.instance_id,
            gateway_instance_started_at=self._started_at,
        )
        self._counters["requests"] += 1
        # 到达即落一行。日志若只在完成时插入，超过 60 秒的请求和进行中的请求都不会进入当前 QPS。
        await self._persist(record, finished=False)
        key: KeyConfig | None = None
        key_lease: str | None = None
        deferred = False
        try:
            # 先鉴权再读取请求体：无效 Key 不消费请求体，未授权请求不触发大体积读取。
            key = self.authenticate(request.headers.get("authorization"), config=config)
            record.key_id = key.id
            record.owner_id = key.owner_id
            # 共享吊销状态必须在读取请求体与调度之前查询：已吊销（或状态不可用）的 Key
            # 不消费请求体、不接触下游。
            await self._check_revocation(key)

            audio = None
            body = await self._read_bounded_body(
                request, limit=MAX_AUDIO_BODY_BYTES if path == AUDIO_PATH else MAX_REQUEST_BODY_BYTES
            )
            if path == AUDIO_PATH:
                audio = parse_audio_upload(body, request.headers.get("content-type", ""))
                payload = audio.fields
            else:
                payload = self._parse_body(body)
            model = payload.get("model")
            if not isinstance(model, str) or not model:
                raise invalid_request("请求体缺少 model")
            record.model = model
            record.streaming = False if audio else bool(payload.get("stream"))
            if path == "/v1/embeddings":
                if record.streaming or "input" not in payload:
                    raise invalid_request("嵌入请求需要 input，且不支持 stream")

            if key.allowed_models and model not in key.allowed_models:
                raise key_forbidden(model)

            route = config.routes.get(model)
            if route is None or not route.enabled:
                raise model_not_found(model)
            expected_type = "ASR" if audio else "EMBEDDING" if path == "/v1/embeddings" else "CHAT"
            if route.model_type != expected_type:
                raise invalid_request("模型路由类型与请求接口不匹配")
            pool = config.pools.get(route.pool_id)
            if pool is None or not pool.enabled:
                raise model_not_found(model)
            record.pool_id = pool.id
            actual_model = route.target_model if route.target_model else model
            record.actual_model = actual_model

            # Key 小时/日额度在调度之前判定：达限时不取节点租约、不发上游请求。
            await self._check_key_quota(key)

            if not self._runtime_healthy:
                raise runtime_unavailable()

            try:
                key_lease = await self.runtime.acquire(
                    SCOPE_KEY, key.id, key.max_concurrency, self._lease_ttl
                )
            except Exception as exc:  # noqa: BLE001 - Redis 失败不得退化为进程内计数
                raise runtime_unavailable() from exc
            if key_lease is None:
                raise key_concurrency_exceeded()
            self._counters["active"] += 1

            tried: set[str] = set()
            for attempt in range(self._max_retries + 1):
                node, node_lease, reasons = await self._pick_node(
                    config=config, model=actual_model, pool=pool, exclude=tried
                )
                if node is None:
                    raise self._no_node_error(reasons)
                record.node_id = node.id
                # 被动健康信号也落在连接作用域：只有当前请求使用的连接的成败才影响调度。
                node_scope = node.runtime_scope
                try:
                    api_key = self._decode_node_key(node)
                    headers = build_upstream_headers(request.headers, node, api_key)
                    if audio:
                        upstream_body, headers["content-type"] = audio.encode(actual_model)
                    elif actual_model != model:
                        upstream_payload = dict(payload)
                        upstream_payload["model"] = actual_model
                        upstream_body = json.dumps(upstream_payload).encode("utf-8")
                    else:
                        upstream_body = body
                    upstream_request = self.proxy.build_request(
                        node, "POST", path, headers=headers, content=upstream_body
                    )
                    # 额外上游尝试从真正发出的那一次起算。解密或组请求失败都不算。
                    if attempt > 0:
                        record.retry_count = attempt
                    # 鉴权、模型和节点都已知后再更新同一行，上游还没返回时节点筛选就能看到它。
                    await self._persist(record, finished=False)
                    if record.streaming:
                        upstream = await self.proxy.send(upstream_request, stream=True)
                        record.ttfb_ms = elapsed_ms(started)
                        record.status_code = upstream.status_code
                        if 200 <= upstream.status_code < 300 and not _is_event_stream(upstream):
                            # 2xx 却不是 SSE：不能把普通响应伪装成流成功。
                            await upstream.aclose()
                            await self._safe_release(SCOPE_NODE, node.id, node_lease)
                            await self._record_node_failure(node_scope)
                            raise upstream_protocol_error()
                        # 流式响应在生成器结束时才释放租约与落日志；长流周期续租。
                        deferred = True
                        return self._streaming_response(
                            upstream, record, key, node.id, node_scope, node_lease or "", key_lease
                        )
                    upstream = await self.proxy.send(upstream_request, stream=False)
                    record.ttfb_ms = elapsed_ms(started)
                    record.status_code = upstream.status_code
                    if audio and upstream.is_success:
                        try:
                            valid_text = isinstance(upstream.json().get("text"), str)
                        except (ValueError, AttributeError):
                            valid_text = False
                        if not valid_text:
                            record.status_code = 502
                            await self._record_node_failure(node_scope)
                            raise upstream_protocol_error()
                    # 语音转写的 4xx 是客户端音频问题，不应让上游节点降级或开熔断；
                    # 鉴权、限流和 5xx 仍然计入节点失败。
                    node_failure = not audio or upstream.status_code >= 500 or (
                        upstream.status_code in {401, 403, 429}
                    )
                    await self._complete_non_stream(
                        record, upstream, node_scope, node_failure=node_failure
                    )
                    await self._safe_release(SCOPE_NODE, node.id, node_lease)
                    response_content = upstream.content
                    if actual_model != model and 200 <= upstream.status_code < 300:
                        try:
                            resp_json = upstream.json()
                            if isinstance(resp_json, dict):
                                resp_json["model"] = model
                                response_content = json.dumps(resp_json).encode("utf-8")
                        except Exception:
                            pass
                    return Response(
                        content=response_content,
                        status_code=upstream.status_code,
                        headers=self._forward_response_headers(upstream),
                    )
                except NotSentDownstream as exc:
                    await self._safe_release(SCOPE_NODE, node.id, node_lease)
                    await self._record_node_failure(node_scope)
                    error = self.proxy.classify(exc)
                    record.error_code = error.code
                    tried.add(node.id)
                    if attempt < self._max_retries:
                        continue
                    raise error from exc
                except GatewayDataPlaneError:
                    await self._safe_release(SCOPE_NODE, node.id, node_lease)
                    raise
                except Exception:
                    await self._safe_release(SCOPE_NODE, node.id, node_lease)
                    await self._record_node_failure(node_scope)
                    raise
        except GatewayDataPlaneError as exc:
            record.status_code = record.status_code or exc.status_code
            record.error_code = exc.code
            self._counters["errors"] += 1
            raise
        except Exception as exc:  # noqa: BLE001 - 传输异常统一分类
            error = self.proxy.classify(exc)
            record.status_code = record.status_code or error.status_code
            record.error_code = error.code
            self._counters["errors"] += 1
            raise error from exc
        finally:
            if not deferred:
                if key_lease is not None and key is not None:
                    await self._safe_release(SCOPE_KEY, key.id, key_lease)
                    self._counters["active"] = max(0, self._counters["active"] - 1)
                await self._persist(record, finished=True)

    # ── 鉴权与路由 ─────────────────────────────────────────────────────

    async def _check_key_quota(self, key: KeyConfig) -> None:
        limits = {
            "hour": key.hourly_token_limit,
            "day": key.daily_token_limit,
        }
        if not any(limit and limit > 0 for limit in limits.values()):
            return
        buckets = usage_buckets(utcnow(), self._timezone)
        for period, limit in limits.items():
            if not limit or limit <= 0:
                continue
            try:
                usage = await self.runtime.usage(SCOPE_KEY, key.id, period, buckets[period])
            except Exception as exc:  # noqa: BLE001 - 运行态不可用时按 503 拒绝
                raise runtime_unavailable() from exc
            if usage.billable_total >= limit:
                raise key_quota_exceeded(period)

    def authenticate(self, authorization: str | None, *, config: GatewayConfig) -> KeyConfig:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise unauthorized()
        token = authorization[7:].strip()
        if not token:
            raise unauthorized()
        key = config.keys.get(gateway_key_digest(token))
        if key is None:
            raise unauthorized()
        if not key.enabled or key.revoked_at is not None:
            raise key_disabled()
        if key.expires_at is not None and key.expires_at <= utcnow():
            raise key_expired()
        return key

    async def _read_bounded_body(
        self, request: Request, *, limit: int = MAX_REQUEST_BODY_BYTES
    ) -> bytes:
        """有界读取请求体：先按 Content-Length 拒绝，再按实际流字节强制上限。

        不缓存到 ``request._body``，也不回显请求体；超限时抛结构化 413，交由统一
        异常处理返回 OpenAI 兼容错误体。
        """
        message = f"请求体超过 {limit} 字节上限"
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                content_length = int(declared)
            except ValueError:
                content_length = None
            if content_length is not None and content_length > limit:
                raise payload_too_large(message)

        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise payload_too_large(message)
            chunks.append(chunk)
        return b"".join(chunks)

    def _parse_body(self, body: bytes) -> dict[str, Any]:
        if not body:
            raise invalid_request("请求体为空")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise invalid_request("请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise invalid_request("请求体必须是 JSON 对象")
        return payload

    def _decode_node_key(self, node: NodeConfig) -> str | None:
        if not node.api_key_encrypted:
            return None
        try:
            return self.cipher.decrypt(node.api_key_encrypted)
        except CredentialError as exc:
            raise GatewayDataPlaneError(
                NODE_CREDENTIAL_ERROR, "节点凭据无法解密（未配置主密钥）", 503
            ) from exc

    async def _pick_node(
        self, *, config: GatewayConfig, model: str, pool: PoolConfig, exclude: set[str]
    ) -> tuple[NodeConfig | None, str | None, dict[str, int]]:
        try:
            return await self._pick_node_from_runtime(
                config=config, model=model, pool=pool, exclude=exclude
            )
        except GatewayDataPlaneError:
            raise
        except Exception as exc:  # noqa: BLE001 - Redis 失败统一结构化 503
            raise runtime_unavailable() from exc

    async def _pick_node_from_runtime(
        self, *, config: GatewayConfig, model: str, pool: PoolConfig, exclude: set[str]
    ) -> tuple[NodeConfig | None, str | None, dict[str, int]]:
        reasons: dict[str, int] = {}
        candidates: list[Candidate] = []
        for node in config.nodes.values():
            if node.pool_id != pool.id or node.id in exclude:
                continue
            # 健康/熔断按「请求使用的连接指纹」作用域读取：旧实例或旧连接的探测结果
            # 落在别的键，无法授权当前配置；缺失运行态即为 UNKNOWN，绝不回退快照。
            schedule = await read_node_schedule(self.runtime, node, timezone=self._timezone)
            reason = node_block_reason(
                node,
                model=model,
                active=schedule.active,
                circuit=schedule.circuit,
                hourly=schedule.hourly,
                daily=schedule.daily,
                health_state=schedule.health,
                half_open_trials=schedule.half_open_trials,
            )
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            if schedule.circuit == CIRCUIT_HALF_OPEN and not await self.runtime.try_circuit_trial(
                schedule.scope
            ):
                # OPEN 到期后只放行少量试探；试探名额用尽时按熔断打开处理。
                reasons[REASON_CIRCUIT_OPEN] = reasons.get(REASON_CIRCUIT_OPEN, 0) + 1
                continue
            candidates.append(
                Candidate(
                    node,
                    schedule.active,
                    effective_weight(
                        node, schedule.hourly, schedule.daily, health_state=schedule.health
                    ),
                )
            )
        for candidate in order_candidates(candidates):
            acquired = await self.runtime.acquire(
                SCOPE_NODE, candidate.node.id, candidate.node.max_concurrency, self._lease_ttl
            )
            if acquired is not None:
                return candidate.node, acquired, reasons
        if candidates and not reasons:
            reasons[REASON_NODE_FULL] = len(candidates)
        return None, None, reasons

    def _no_node_error(self, reasons: Mapping[str, int]) -> GatewayDataPlaneError:
        detail = {key: value for key, value in reasons.items()}
        if reasons.get(REASON_NODE_FULL):
            return no_eligible_node(POOL_SATURATED, detail)
        if reasons.get(REASON_QUOTA_EXCEEDED):
            return no_eligible_node(QUOTA_EXCEEDED, detail)
        return no_eligible_node(NO_ELIGIBLE_NODE, detail)

    @staticmethod
    def _forward_response_headers(response: httpx.Response) -> dict[str, str]:
        return {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _STRIPPED_RESPONSE_HEADERS
        }

    # ── 转发与计量 ─────────────────────────────────────────────────────

    async def _complete_non_stream(
        self,
        record: RequestRecord,
        response: httpx.Response,
        node_scope: str,
        *,
        node_failure: bool = True,
    ) -> None:
        usage = self._json_usage(response)
        record.duration_ms = elapsed_ms(record.started_at)
        await self._record_usage(record, usage)
        if 200 <= response.status_code < 400:
            await self._record_node_success(node_scope)
        elif node_failure:
            await self._record_node_failure(node_scope)
            record.error_code = record.error_code or f"UPSTREAM_{response.status_code}"
        else:
            # 客户端输入错误既不奖励也不惩罚节点。
            record.error_code = record.error_code or f"UPSTREAM_{response.status_code}"

    def _json_usage(self, response: httpx.Response) -> dict[str, Any] | None:
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            return payload["usage"]
        return None

    async def _record_usage(self, record: RequestRecord, usage: Mapping[str, Any] | None) -> None:
        if usage is None:
            record.output_tps = None
            await self._record_usage_scopes(record, None, known=False)
            return
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        record.input_tokens = input_tokens
        record.output_tokens = output_tokens
        record.total_tokens = total_tokens
        is_success = (
            not record.stream_interrupted
            and record.status_code is not None
            and 200 <= record.status_code < 300
        )
        if (
            record.streaming
            and is_success
            and record.output_tokens is not None
            and record.output_tokens > 1
            and record.duration_ms is not None
            and record.ttft_ms is not None
            and record.duration_ms > record.ttft_ms
        ):
            seconds = (record.duration_ms - record.ttft_ms) / 1000.0
            record.output_tps = round(record.output_tokens / seconds, 3) if seconds > 0 else None
        else:
            record.output_tps = None
        await self._record_usage_scopes(record, (input_tokens, output_tokens, total_tokens), True)

    async def _record_usage_scopes(
        self,
        record: RequestRecord,
        tokens: tuple[int, int, int] | None,
        known: bool,
    ) -> None:
        buckets = usage_buckets(utcnow(), self._timezone)
        input_tokens, output_tokens, total_tokens = tokens or (0, 0, 0)
        targets = []
        if record.node_id:
            targets.append((SCOPE_NODE, record.node_id))
        if record.key_id:
            targets.append((SCOPE_KEY, record.key_id))
        for scope, scope_id in targets:
            for period in ("hour", "day"):
                try:
                    await self.runtime.record_usage(
                        scope,
                        scope_id,
                        period,
                        buckets[period],
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=total_tokens,
                        known=known,
                    )
                except Exception:  # noqa: BLE001 - 用量计入失败不阻断已完成的响应
                    pass

    async def _safe_release(self, scope: str, scope_id: str, lease_id: str | None) -> None:
        if not lease_id:
            return
        try:
            await self.runtime.release(scope, scope_id, lease_id)
        except Exception:  # noqa: BLE001 - 释放失败交由租约 TTL 回收
            pass

    async def _record_node_success(self, node_scope: str | None) -> None:
        # node_scope 是「节点 ID + 连接指纹」，被动成功只关闭当前请求使用的连接的熔断。
        if not node_scope:
            return
        try:
            transition = await self.runtime.transition_circuit_closed(node_scope)
        except Exception:  # noqa: BLE001
            return
        if transition.changed:
            await self._write_circuit_event(
                node_scope, transition.previous, transition.current, "circuit_closed"
            )

    async def _record_node_failure(self, node_scope: str | None) -> None:
        if not node_scope:
            return
        try:
            transition = await self.runtime.transition_circuit_open(
                node_scope, self._circuit_open_seconds
            )
            await self.runtime.mark_degraded(node_scope)
        except Exception:  # noqa: BLE001
            return
        if transition.changed:
            await self._write_circuit_event(
                node_scope, transition.previous, transition.current, "circuit_open"
            )

    async def _write_circuit_event(
        self, node_scope: str, previous: str, current: str, kind: str
    ) -> None:
        """只在熔断状态变化时记一条。已经打开或已经关闭时不再为每次失败重复写。"""

        node_id = node_scope.split("#", 1)[0]
        label = "熔断打开" if kind == "circuit_open" else "熔断恢复"
        try:
            async with self._session_factory() as session:
                session.add(
                    GatewayRuntimeEvent(
                        source="runtime",
                        kind=kind,
                        node_id=node_id,
                        summary=f"{node_id} {label}",
                        previous=previous,
                        current=current,
                        detail={"scope": node_scope},
                    )
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 事件落库失败不改变已经写入的熔断状态
            pass

    # ── 租约续租 ───────────────────────────────────────────────────────

    def _start_lease_renewals(self, *leases: tuple[str, str, str | None]) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []
        for scope, scope_id, lease_id in leases:
            if lease_id:
                tasks.append(asyncio.create_task(self._renew_loop(scope, scope_id, lease_id)))
        return tasks

    async def _renew_loop(self, scope: str, scope_id: str, lease_id: str) -> None:
        interval = max(1.0, self._lease_ttl / 3)
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self.runtime.renew(scope, scope_id, lease_id, self._lease_ttl)
                if not renewed:
                    return
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - 续租失败由租约 TTL 与后续请求兜底
            return

    @staticmethod
    async def _cancel_renewals(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _streaming_response(
        self,
        upstream: httpx.Response,
        record: RequestRecord,
        key: KeyConfig,
        node_id: str,
        node_scope: str,
        node_lease: str | None,
        key_lease: str | None,
    ) -> StreamingResponse:
        renewals = self._start_lease_renewals(
            (SCOPE_NODE, node_id, node_lease),
            (SCOPE_KEY, key.id, key_lease),
        )
        return StreamingResponse(
            self._stream_body(
                upstream, record, key, node_id, node_scope, node_lease, key_lease, renewals
            ),
            status_code=upstream.status_code,
            headers=self._forward_response_headers(upstream),
        )

    async def _stream_body(
        self,
        upstream: httpx.Response,
        record: RequestRecord,
        key: KeyConfig,
        node_id: str,
        node_scope: str,
        node_lease: str | None,
        key_lease: str | None,
        renewals: list[asyncio.Task[None]],
    ) -> AsyncIterator[bytes]:
        collector = StreamUsageCollector()
        completed = False
        disconnect_code: str | None = None
        rewriter = (
            StreamModelRewriter(record.model)
            if record.model and record.actual_model and record.actual_model != record.model
            else None
        )
        try:
            async for chunk in upstream.aiter_bytes():
                saw_content = collector.feed(chunk)
                if record.ttft_ms is None and saw_content:
                    record.ttft_ms = elapsed_ms(record.started_at)
                yield rewriter.feed(chunk) if rewriter else chunk
            if rewriter:
                tail = rewriter.flush()
                if tail:
                    yield tail
            completed = True
        except httpx.HTTPError:
            disconnect_code = STREAM_INTERRUPTED
            raise
        except (asyncio.CancelledError, GeneratorExit):
            disconnect_code = CLIENT_DISCONNECTED
            raise
        finally:
            await asyncio.shield(
                self._finalize_stream(
                    upstream,
                    record,
                    key,
                    node_id,
                    node_scope,
                    node_lease,
                    key_lease,
                    renewals,
                    collector,
                    completed,
                    disconnect_code,
                )
            )

    async def _finalize_stream(
        self,
        upstream: httpx.Response,
        record: RequestRecord,
        key: KeyConfig,
        node_id: str,
        node_scope: str,
        node_lease: str | None,
        key_lease: str | None,
        renewals: list[asyncio.Task[None]],
        collector: StreamUsageCollector,
        completed: bool,
        disconnect_code: str | None,
    ) -> None:
        record.duration_ms = elapsed_ms(record.started_at)
        ok_status = 200 <= upstream.status_code < 300
        if not completed:
            record.stream_interrupted = True
            record.error_code = record.error_code or disconnect_code or CLIENT_DISCONNECTED
            self._counters["errors"] += 1
        elif upstream.status_code < 200 or upstream.status_code >= 400:
            record.error_code = record.error_code or f"UPSTREAM_{upstream.status_code}"
        elif not collector.saw_content:
            # 200 + 只有结束标记：不是成功，也不能作为节点健康信号。
            record.stream_interrupted = True
            record.error_code = record.error_code or STREAM_EMPTY
            self._counters["errors"] += 1
        elif not collector.saw_terminal:
            # 没有 [DONE]/finish_reason 就 EOF：按意外断流处理。
            record.stream_interrupted = True
            record.error_code = record.error_code or STREAM_INCOMPLETE
            self._counters["errors"] += 1
        await self._cancel_renewals(renewals)
        try:
            await upstream.aclose()
        except Exception:  # noqa: BLE001
            pass
        await self._record_usage(record, collector.usage)
        if (
            completed
            and not disconnect_code
            and ok_status
            and collector.saw_content
            and collector.saw_terminal
        ):
            await self._record_node_success(node_scope)
        else:
            await self._record_node_failure(node_scope)
        await self._safe_release(SCOPE_NODE, node_id, node_lease)
        await self._safe_release(SCOPE_KEY, key.id, key_lease)
        self._counters["active"] = max(0, self._counters["active"] - 1)
        await self._persist(record, finished=True)

    # ── 日志 ───────────────────────────────────────────────────────────

    async def _persist(self, record: RequestRecord, *, finished: bool) -> None:
        try:
            async with self._session_factory() as session:
                incoming = record.to_row(finished=finished)
                existing = await session.get(GatewayRequest, record.id)
                if existing is None:
                    session.add(incoming)
                    stored = incoming
                else:
                    for name in _REQUEST_COLUMNS:
                        setattr(existing, name, getattr(incoming, name))
                    stored = existing
                # 调用日志能看到 Key，但密钥页的「最近使用」只读 gateway_keys.last_used_at。
                # 只有完成的请求才更新最近使用；到达时的未完成行还没有结束时间。
                if finished and record.key_id:
                    key = await session.get(GatewayKey, record.key_id)
                    used_at = stored.finished_at
                    if key is not None and used_at is not None and (
                        key.last_used_at is None or key.last_used_at < used_at
                    ):
                        key.last_used_at = used_at
                await session.commit()
        except Exception:  # noqa: BLE001 - 日志失败不影响已发送的响应
            pass


def create_gateway_app(data_plane: GatewayDataPlane) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await data_plane.startup()
        try:
            yield
        finally:
            await data_plane.shutdown()

    application = FastAPI(title="Nexus API Gateway", version="0.4.0", lifespan=lifespan)

    @application.exception_handler(GatewayDataPlaneError)
    async def _handle(_: Request, exc: GatewayDataPlaneError) -> JSONResponse:
        return exc.to_response()

    @application.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "instance_id": data_plane.instance_id,
                "runtime_healthy": data_plane.runtime_healthy,
            }
        )

    @application.get(MODELS_PATH)
    async def list_models(request: Request) -> JSONResponse:
        return await data_plane.handle_models(request)

    @application.post(CHAT_PATH)
    async def chat_completions(request: Request) -> Response:
        return await data_plane.handle_chat(request)

    @application.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        return await data_plane.handle_embeddings(request)

    @application.post(AUDIO_PATH)
    async def transcriptions(request: Request) -> Response:
        return await data_plane.handle_transcriptions(request)

    return application


def build_default_data_plane() -> GatewayDataPlane:
    settings = get_settings()
    return GatewayDataPlane(
        config_state=GatewayConfigState(),
        runtime=RedisRuntimeStore(settings.redis_url),
        proxy=GatewayProxy(default_connect_timeout=10.0, default_request_timeout=600.0),
        cipher=CredentialCipher(settings.gateway_master_key),
        session_factory=SessionLocal,
        config_loader=ConfigLoader(SessionLocal),
        instance_id=settings.gateway_instance_id,
        heartbeat_interval=settings.gateway_heartbeat_interval,
        heartbeat_ttl=settings.gateway_heartbeat_ttl,
        lease_ttl=settings.gateway_lease_ttl,
        config_poll_interval=settings.gateway_config_poll_interval,
        runtime_health_interval=settings.gateway_runtime_health_interval,
        revocation_sync_interval=settings.gateway_revocation_sync_interval,
        timezone=settings.timezone,
        probe_client=HttpGatewayProbeClient(
            settings.gateway_probe_timeout_seconds,
            default_health_path=settings.gateway_health_path,
        ),
        probe_interval=settings.gateway_probe_interval_seconds,
    )


app = create_gateway_app(build_default_data_plane())
