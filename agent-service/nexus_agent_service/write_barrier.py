# -*- coding: utf-8 -*-
"""原生 Agent 写屏障（纯 ASGI middleware）。

背景：官方 ``PATCH /agent/{id}`` 的执行序列是
``resolve_for_edit → merge → await upsert_agent → await SQL commit → 返回
AgentView``，没有后台写任务。若客户端在超时/断开后取消外层请求，原生
``upsert_agent`` 可能仍在进行；随后到来的新编辑会先提交，而原请求稍后才落库，
导致后写的旧内容覆盖新内容。

本 middleware 只在官方应用之上、路由之外工作，不改动官方路由与核心源码：

* 对同一 ``/agent/{id}`` 的原生 ``PATCH``、``DELETE`` 按 ``agent_id`` 串行化
  **完整下游请求生命周期**（含读取 body 与 DB 提交）；
* 在读取 body 之前先取得进程内业务 ``asyncio.Lock``；
* 若外层请求被取消（客户端超时/断开），**shield** 实际下游 app 任务，等待实际
  任务真正退出后才释放锁，使后续写请求必须排队到真实提交之后；
* 在锁内用公开 ``StorageBase.get_agent`` 读取已持久化的 prompt 修订号，结合本
  进程已接受的高水位，拒绝低于当前修订的写请求（``409``），作为第二道防线；
  读取失败无法证明非托管，返回脱敏 ``503`` 并停止原生 PATCH（fail-closed）；
* 修订只认编译 prompt 末尾规范的“员工 ID 标记行 + 修订标记最后一行”对，普通
  职责/指令正文含相同标记不参与解析，且目标员工标记必须与当前员工一致；
* 托管 ``POST /agent/`` 携带预检服务代次头（``X-Nexus-Service-Generation``），
  与当前实例代次不一致时返回 ``409`` 且不执行原生创建，避免旧代次请求在新
  实例上创建原生 Agent；
* 不带 Nexus 员工标记的原生独立 CRUD 探针维持官方行为。

仅依赖官方公开 AGENTSCOPE API（``StorageBase``）与 ASGI 规范，不导入任何官方
私有模块（``_service`` / ``_router`` 等）。
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

# 与 ``backend/nexus_backend/employees.py`` 保持一致的**最小**关联标记：
# 稳定员工 ID 标记用于识别 Nexus 托管员工，独立修订标记用于写屏障校验。
EMPLOYEE_MARKER_PREFIX = "[nexus-employee-id:"
REVISION_MARKER_PREFIX = "[nexus-write-revision:"
MARKER_SUFFIX = "]"

# 公开 HTTP 头：业务 PATCH 携带目标 prompt 修订号，值必须与目标 prompt 内的
# 修订标记一致，避免误标。
REVISION_HEADER = b"x-nexus-write-revision"
USER_HEADER = b"x-user-id"
# 公开 HTTP 头：Nexus 托管创建携带创建预检得到的服务代次；服务在任何托管
# POST 写入前与当前实例代次比较，不一致则明确拒绝且不执行原生创建。
GENERATION_HEADER = b"x-nexus-service-generation"

AGENT_COLLECTION_PATH = "/agent/"
# 托管 Session 创建同样携带服务代次头：预检后服务重启时旧代次延迟 POST 必须
# 被拒绝，避免在新实例创建孤立原生 Session。
SESSION_COLLECTION_PATH = "/sessions/"
_MANAGED_CREATE_PATHS = frozenset({AGENT_COLLECTION_PATH, SESSION_COLLECTION_PATH})
_AGENT_PATH = re.compile(r"^/agent/([^/]+)$")


def _exact_marker_line(line: str, prefix: str) -> str | None:
    """整行恰为 ``prefix...]`` 标记时返回内部值，否则 ``None``。"""
    if not line.startswith(prefix) or not line.endswith(MARKER_SUFFIX):
        return None
    value = line[len(prefix) : -len(MARKER_SUFFIX)]
    return value or None


def parse_managed_markers(prompt: str | None) -> tuple[str | None, int | None]:
    """只接受编译 prompt 末尾规范的“员工 ID 标记行 + 修订标记最后一行”对。

    普通职责/指令正文即使含相同标记文本也不参与解析：只认末尾两行，避免
    正文误命中导致修订被误解析为更小/更大值。
    """
    if not isinstance(prompt, str):
        return None, None
    trimmed = prompt.rstrip()
    if not trimmed:
        return None, None
    lines = trimmed.split("\n")
    if len(lines) < 2:
        return None, None
    employee_id = _exact_marker_line(
        lines[-2].strip(),
        EMPLOYEE_MARKER_PREFIX,
    )
    revision_raw = _exact_marker_line(
        lines[-1].strip(),
        REVISION_MARKER_PREFIX,
    )
    if employee_id is None or revision_raw is None or not revision_raw.isdigit():
        return None, None
    return employee_id, int(revision_raw)


def parse_revision(prompt: str | None) -> int | None:
    """从规范末尾标记解析修订；无规范标记返回 ``None``。"""
    _, revision = parse_managed_markers(prompt)
    return revision


def is_managed_prompt(prompt: str | None) -> bool:
    """是否 Nexus 托管员工（末尾带规范的员工 ID + 修订标记对）。"""
    employee_id, _ = parse_managed_markers(prompt)
    return employee_id is not None


def _agent_id(scope: dict) -> str | None:
    match = _AGENT_PATH.match(scope.get("path", ""))
    return match.group(1) if match else None


def _header(scope: dict, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _prompt_of(record: Any) -> str | None:
    data = getattr(record, "data", None)
    prompt = getattr(data, "system_prompt", None)
    return prompt if isinstance(prompt, str) else None


async def _buffer_body(
    receive: Callable[[], Awaitable[dict]],
) -> tuple[bytes, Callable[[], Awaitable[dict]]]:
    """在锁内完整读取请求 body，并返回可重放的 ``receive``。"""
    body = bytearray()
    disconnected = False
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            disconnected = True
            break
        if message["type"] != "http.request":
            continue
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break

    state = {"replayed": False}

    async def replay() -> dict:
        if not state["replayed"]:
            state["replayed"] = True
            if disconnected:
                return {"type": "http.disconnect"}
            return {
                "type": "http.request",
                "body": bytes(body),
                "more_body": False,
            }
        return await receive()

    return bytes(body), replay


async def _send_json(
    send: Callable[[dict], Awaitable[None]],
    status: int,
    detail: str,
) -> None:
    payload = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        },
    )
    await send({"type": "http.response.body", "body": payload})


async def await_real_task(task: "asyncio.Task[Any]") -> None:
    """等待实际下游任务退出；外层取消时先等真实任务结束再如实抛出。

    供写屏障与运行 admission 共用：shield 保证外层取消不会打断真实下游
    任务，只有它真正退出后才释放栅栏，避免「已受理但尚未登记」的请求被漏掉。
    """
    outer_cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # 外层被取消：shield 保证 task 继续，记住取消并等待其真实退出。
            outer_cancelled = exc
            continue
        except Exception:
            break
    if task.cancelled():
        raise asyncio.CancelledError
    exc = task.exception()
    if exc is not None:
        raise exc
    if outer_cancelled is not None:
        # 真实写入已完成并释放锁后，再如实向上传播外层取消。
        raise outer_cancelled


class NativeWriteBarrierMiddleware:
    """按 ``agent_id`` 串行原生 PATCH/DELETE 并校验单调修订的 ASGI 中间件。

    仅在本项目单进程部署语义下提供保证：锁与高水位都保存在进程内存中，不做
    跨副本协调，也不引入后台编排或 operation 表。
    """

    def __init__(
        self,
        app: Any,
        *,
        storage: Any,
        user_id: str,
        generation_id: str | None = None,
    ) -> None:
        self.app = app
        self._storage = storage
        self._user_id = user_id
        # 本实例的服务代次，用于拒绝预检后因重启而错代的托管创建 POST。
        self._generation_id = generation_id
        self._locks: dict[str, asyncio.Lock] = {}
        # 已接受高水位：全局键 ``user_id:agent_id`` → 已受理的最大修订。
        self._high_water: dict[str, int] = {}

    def _lock_for(self, agent_id: str) -> asyncio.Lock:
        lock = self._locks.get(agent_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[agent_id] = lock
        return lock

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        if method == "POST" and scope.get("path") in _MANAGED_CREATE_PATHS:
            await self._handle_managed_create(scope, receive, send)
            return
        agent_id = _agent_id(scope) if method in ("PATCH", "DELETE") else None
        if agent_id is None:
            await self.app(scope, receive, send)
            return

        lock = self._lock_for(agent_id)
        await lock.acquire()
        try:
            task = asyncio.ensure_future(
                self._handle(scope, receive, send, agent_id, method),
            )
            await self._await_real_task(task)
        finally:
            lock.release()

    async def _await_real_task(self, task: "asyncio.Task[Any]") -> None:
        """等待实际下游任务退出；外层取消时先等真实任务结束再如实抛出。"""
        await await_real_task(task)

    async def _handle_managed_create(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
    ) -> None:
        """托管 Agent/Session 创建必须携带与当前实例一致的代次头。

        未带代次头是独立原生创建探针，维持官方行为；带了但与当前实例不一致，
        说明预检后服务已重启，旧代次请求不得在新实例创建原生 Agent/Session。
        """
        generation = _header(scope, GENERATION_HEADER)
        if generation is None or not generation.strip():
            await self.app(scope, receive, send)
            return
        if (
            self._generation_id is None
            or generation.strip() != self._generation_id
        ):
            await _send_json(
                send,
                409,
                "服务代次不匹配：创建请求未在当前服务代次受理，已拒绝且未执行"
                "原生创建，请重试。",
            )
            return
        await self.app(scope, receive, send)

    async def _handle(
        self,
        scope: dict,
        receive: Callable[[], Awaitable[dict]],
        send: Callable[[dict], Awaitable[None]],
        agent_id: str,
        method: str,
    ) -> None:
        if method == "PATCH":
            body, upstream_receive = await _buffer_body(receive)
            status, detail = await self._check_revision(
                scope,
                agent_id,
                body,
            )
            if status is not None:
                await _send_json(send, status, detail or "已拒绝。")
                return
        else:
            upstream_receive = receive

        await self.app(scope, upstream_receive, send)

    def _high_water_key(self, scope: dict, agent_id: str) -> str:
        user_id = _header(scope, USER_HEADER) or self._user_id
        return f"{user_id}:{agent_id}"

    async def _check_revision(
        self,
        scope: dict,
        agent_id: str,
        body: bytes,
    ) -> tuple[int | None, str | None]:
        """校验受管 PATCH 的修订。

        返回 ``(状态码, 拒绝原因)``：状态码为 ``None`` 时放行（非受管，保持
        官方行为，或已通过校验）；非 ``None`` 时以该状态码明确拒绝。
        """
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        if not isinstance(payload, dict):
            return None, None

        user_id = _header(scope, USER_HEADER) or self._user_id
        try:
            record = await self._storage.get_agent(user_id, agent_id)
        except Exception:  # noqa: BLE001 - 读取失败必须 fail-closed
            # 读取失败无法证明该记录非托管；若放行，下一次读取成功时低修订会
            # 真实覆盖新编辑。返回脱敏 503 并停止原生 PATCH。
            return (
                503,
                "无法确认员工关联状态，已拒绝受管写入以避免旧编辑覆盖。",
            )

        persisted_prompt = _prompt_of(record)
        persisted_employee_id, persisted_revision = parse_managed_markers(
            persisted_prompt,
        )
        if persisted_employee_id is None:
            # 非 Nexus 托管员工：原生独立 CRUD 探针维持官方行为。
            return None, None

        header_revision = _header(scope, REVISION_HEADER)
        if header_revision is None or not header_revision.strip():
            return 409, "受管员工写请求缺少写入修订头，已拒绝。"
        try:
            request_revision = int(header_revision.strip())
        except ValueError:
            return 409, "受管员工写请求的写入修订头非法，已拒绝。"
        if request_revision < 0:
            return 409, "受管员工写请求的写入修订头非法，已拒绝。"

        target_prompt = payload.get("system_prompt")
        target_employee_id, target_revision = parse_managed_markers(
            target_prompt,
        )
        if target_employee_id is None:
            return 409, "受管员工写请求的目标 prompt 缺少规范员工标记，已拒绝。"
        if target_employee_id != persisted_employee_id:
            # 修订写入只能更新同一稳定员工，不允许替换为另一员工标记。
            return 409, "目标 prompt 员工标记与当前员工不一致，已拒绝。"
        if target_revision is None:
            return 409, "受管员工写请求的目标 prompt 缺少修订标记，已拒绝。"
        if target_revision != request_revision:
            return 409, "写入修订头与目标 prompt 修订不一致，已拒绝。"

        key = self._high_water_key(scope, agent_id)
        high_water = self._high_water.get(key, 0)
        current = max(high_water, persisted_revision or 0)
        if request_revision < current:
            return (
                409,
                "写入修订低于服务已接受修订，已拒绝以避免旧请求覆盖新编辑。",
            )
        # 通过受管修订校验、进入原生写入前即提升已接受高水位：同修订允许重试，
        # 而即便本次原生返回 4xx/5xx，后续低修订也不能迟到覆盖（后端本地修订
        # 计数同样不回退；服务重启再从原生持久修订恢复）。
        self._high_water[key] = max(high_water, request_revision)
        return None, None
