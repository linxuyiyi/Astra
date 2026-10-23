"""Worker Gateway 服务。

持有套接字的进程是连接所有者。其他进程只通过 RouteBus 投递已落库的指令。
认证成功会使 epoch 加一，旧连接的断开和迟到响应不能改写新 epoch 的状态。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from secrets import token_hex, token_urlsafe
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocketDisconnect

from app.config.settings import Settings, get_settings
from app.database import SessionLocal
from app.gateway.security import CredentialCipher, CredentialError, gateway_key_digest, verify_gateway_key
from app.models import (
    OpenCodeExecutionEnvironment,
    WorkerCommand,
    WorkerConnection,
    WorkerInstance,
    utcnow,
)
from app.worker_agent.dsh_acp.commands import MUTATIONS as DSH_MUTATIONS
from app.worker_gateway.bus import ConnectionQueues, build_route_bus
from app.worker_gateway.data_channel import DataChannel, http_frame_data, side_effect_http
from app.worker_gateway.heartbeat import HeartbeatMonitor
from app.worker_gateway.http_limit import release_http_slot, reserve_http_slot
from app.worker_gateway.protocol import (
    WorkerGatewayError,
    WorkerMessage,
    dump_message,
    envelope,
    frame_acceptable,
    parse_message,
    validate_command_name,
)

# 有副作用且结果未知时不能盲重发。阶段 1 没有 OpenCode 对账，投递成功后就停在原命令上。
SIDE_EFFECT_COMMANDS = frozenset({"prompt", "upload_file"}) | DSH_MUTATIONS

logger = logging.getLogger(__name__)


def held_slot_expire_statement(command_id: str, due: tuple, values: dict[str, Any]):
    """Conditional expire of one held command. MySQL cannot run UPDATE … RETURNING."""

    return (
        update(WorkerCommand)
        .where(WorkerCommand.id == command_id, WorkerCommand.slot_held.is_(True), *due)
        .values(**values)
    )


def expire_due_commands_statement(due: tuple, values: dict[str, Any]):
    return update(WorkerCommand).where(*due).values(**values)


@dataclass(slots=True)
class AuthOk:
    worker_instance_id: str
    environment_id: int
    epoch: int
    ack: WorkerMessage


@dataclass(slots=True)
class EnrollResult:
    worker_instance_id: str
    environment_id: int
    token: str = field(repr=False)
    epoch: int
    rotated: bool


@dataclass(slots=True)
class DispatchResult:
    command: WorkerCommand
    delivered: bool
    replayed: bool
    error_code: str | None


@dataclass
class _SocketLease:
    epoch: int
    websocket: Any
    inbound_events: deque[dict[str, Any]]
    event_limit: int
    dropped_events: int = 0
    superseded: bool = False

    def buffer_event(self, frame: dict[str, Any]) -> bool:
        if len(self.inbound_events) >= self.event_limit:
            self.dropped_events += 1
            return False
        self.inbound_events.append(frame)
        return True


class WorkerGateway:
    def __init__(self, *, settings: Settings | None = None, bus=None, owner_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.owner_id = owner_id or f"gw_{uuid4().hex[:12]}"
        if bus is None:
            self.bus, self.bus_mode, self.bus_warning = build_route_bus(
                self.settings.worker_gateway_bus,
                self.settings.redis_url,
                control_limit=self.settings.worker_gateway_control_queue_size,
                event_limit=self.settings.worker_gateway_event_queue_size,
            )
            if self.bus_warning:
                logger.warning("Worker Gateway 当前总线是 memory，%s", self.bus_warning)
        else:
            self.bus = bus
            self.bus_mode = getattr(bus, "mode", "memory")
            self.bus_warning = "仅单进程可用" if self.bus_mode == "memory" else None
        self.sockets: dict[str, _SocketLease] = {}
        # Last database epoch observed for a worker. Replaced whenever a read
        # disagrees, so a socket authenticated at an older epoch cannot keep
        # appending after the row moves.
        self._db_epochs: dict[str, int] = {}

    async def enroll(self, session: AsyncSession, environment_id: int) -> EnrollResult:
        environment = await session.get(OpenCodeExecutionEnvironment, environment_id)
        if environment is None:
            raise WorkerGatewayError("unknown_environment", "执行环境不存在")
        token = f"wtk_{token_urlsafe(32)}"
        digest = gateway_key_digest(token)
        token_ciphertext = self._token_ciphertext(token)
        existing = await session.scalar(
            select(WorkerInstance).where(WorkerInstance.environment_id == environment_id)
        )
        now = utcnow()
        if existing is None:
            instance = WorkerInstance(
                id=f"wrk_{token_hex(16)}",
                environment_id=environment_id,
                token_digest=digest,
                token_ciphertext=token_ciphertext,
                status="offline",
                epoch=0,
            )
            session.add(instance)
            await session.commit()
            return EnrollResult(instance.id, environment_id, token, 0, False)

        previous_epoch = existing.epoch
        worker_id = existing.id
        new_epoch = previous_epoch + 1
        session.expire(existing)
        result = await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == worker_id, WorkerInstance.epoch == previous_epoch)
            .values(
                token_digest=digest,
                token_ciphertext=token_ciphertext,
                epoch=new_epoch,
                status="offline",
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            raise WorkerGatewayError("epoch_conflict", "连接注册冲突")
        await self._supersede_connections(session, worker_id, now, "token_rotated")
        await session.commit()
        await self.bus.clear_owner(worker_id, previous_epoch, None)
        self._close_local_socket(worker_id, previous_epoch)
        self._db_epochs[worker_id] = new_epoch
        return EnrollResult(worker_id, environment_id, token, new_epoch, True)

    def _token_ciphertext(self, token: str) -> str | None:
        try:
            cipher = CredentialCipher(self.settings.gateway_master_key)
        except CredentialError:
            return None
        if not cipher.available:
            return None
        return cipher.encrypt(token)

    async def authenticate(self, session: AsyncSession, message: WorkerMessage, token: str) -> AuthOk:
        if message.type != "command" or message.payload.get("name") != "authenticate":
            raise WorkerGatewayError("unauthorized", "认证失败")
        instance = await session.get(WorkerInstance, message.worker_instance_id)
        if instance is None or instance.environment_id != message.environment_id:
            raise WorkerGatewayError("unauthorized", "认证失败")
        if not verify_gateway_key(token, instance.token_digest):
            raise WorkerGatewayError("unauthorized", "认证失败")
        expected = instance.epoch
        new_epoch = expected + 1
        now = utcnow()
        version = message.payload.get("agent_version")
        agent_version = version[:64] if isinstance(version, str) and version else instance.agent_version
        worker_id = instance.id
        environment_id = instance.environment_id
        session.expire(instance)
        result = await session.execute(
            update(WorkerInstance)
            .where(WorkerInstance.id == worker_id, WorkerInstance.epoch == expected)
            .values(
                epoch=new_epoch,
                status="online",
                last_heartbeat_at=now,
                connected_at=now,
                agent_version=agent_version,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            raise WorkerGatewayError("epoch_conflict", "连接注册冲突")
        await self._supersede_connections(session, worker_id, now, "epoch_replaced")
        session.add(
            WorkerConnection(worker_instance_id=worker_id, epoch=new_epoch, state="active", opened_at=now)
        )
        await session.commit()
        self._db_epochs[worker_id] = new_epoch
        ack = envelope(
            request_id=message.request_id,
            worker_instance_id=worker_id,
            environment_id=environment_id,
            session_id=None,
            message_type="response",
            payload={
                "ok": True,
                "epoch": new_epoch,
                "ping_seconds": self.settings.worker_gateway_ping_seconds,
                "ping_timeout_seconds": self.settings.worker_gateway_ping_timeout_seconds,
            },
        )
        return AuthOk(worker_id, environment_id, new_epoch, ack)

    async def disconnect(self, session: AsyncSession, worker_instance_id: str, epoch: int) -> None:
        now = utcnow()
        await session.execute(
            update(WorkerConnection)
            .where(
                WorkerConnection.worker_instance_id == worker_instance_id,
                WorkerConnection.epoch == epoch,
                WorkerConnection.state == "active",
            )
            .values(state="closed", close_reason="disconnected", closed_at=now)
        )
        await session.execute(
            update(WorkerInstance)
            .where(
                WorkerInstance.id == worker_instance_id,
                WorkerInstance.epoch == epoch,
                WorkerInstance.status == "online",
            )
            .values(status="offline", updated_at=now)
        )
        await session.commit()

    async def note_heartbeat(self, session: AsyncSession, worker_instance_id: str, epoch: int) -> bool:
        now = utcnow()
        result = await session.execute(
            update(WorkerInstance)
            .where(
                WorkerInstance.id == worker_instance_id,
                WorkerInstance.epoch == epoch,
                WorkerInstance.status == "online",
            )
            .values(last_heartbeat_at=now, updated_at=now)
        )
        await session.commit()
        return result.rowcount == 1

    async def dispatch(
        self,
        session: AsyncSession,
        *,
        worker_instance_id: str,
        request_id: str,
        name: str,
        payload: dict[str, Any],
        session_id: str | None = None,
        timeout_seconds: int | None = None,
    ) -> DispatchResult:
        validate_command_name(name)
        if not isinstance(payload, dict):
            raise WorkerGatewayError("invalid_message", "payload 必须是对象")
        if name == "http":
            from app.worker_gateway.data_channel import encoded_body

            encoded_body(payload)
        dsh_text = None
        if name == "dsh_submit":
            import hashlib
            dsh_text = payload.get("text")
            if not isinstance(dsh_text, str) or len(dsh_text.encode()) > 8 * 1024 * 1024:
                raise WorkerGatewayError("invalid_message", "DSH 提交内容无效或过大")
            payload = {key: value for key, value in payload.items() if key != "text"}
            payload["text_sha256"] = hashlib.sha256(dsh_text.encode()).hexdigest()
        existing = await session.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
        if existing is not None:
            self._require_same_command(existing, worker_instance_id, name, payload, session_id)
            return DispatchResult(existing, False, True, None)
        instance = await session.get(WorkerInstance, worker_instance_id)
        if instance is None:
            raise WorkerGatewayError("unknown_worker", "Worker 不存在")
        now = utcnow()
        timeout = timeout_seconds or self.settings.worker_gateway_command_timeout_seconds
        frame = envelope(
            request_id=request_id,
            worker_instance_id=worker_instance_id,
            environment_id=instance.environment_id,
            session_id=session_id,
            message_type="command",
            payload={"name": name, "data": self._publish_data(name, payload)},
        )
        frame.epoch = instance.epoch
        frame.expires_at = int((now + timedelta(seconds=timeout)).timestamp())
        try:
            encoded = dump_message(frame).encode("utf-8")
        except TypeError as exc:
            raise WorkerGatewayError("invalid_message", "payload 不能序列化") from exc
        if len(encoded) > self.settings.worker_gateway_max_frame_bytes:
            raise WorkerGatewayError("frame_too_large", "单帧超过限制")
        owner = await self.bus.get_owner(worker_instance_id)
        online = instance.status == "online" and owner is not None and owner[1] == instance.epoch
        command = WorkerCommand(
            request_id=request_id,
            worker_instance_id=worker_instance_id,
            environment_id=instance.environment_id,
            session_id=session_id,
            name=name,
            payload=payload,
            status="pending" if online else "failed",
            epoch=instance.epoch,
            error_code=None if online else "offline",
            deadline_at=now + timedelta(seconds=timeout),
            finished_at=None if online else now,
        )
        session.add(command)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            stored = await session.scalar(
                select(WorkerCommand).where(WorkerCommand.request_id == request_id)
            )
            if stored is None:
                raise
            self._require_same_command(stored, worker_instance_id, name, payload, session_id)
            return DispatchResult(stored, False, True, None)
        if name == "http":
            await DataChannel(self).stage_request(command)
        if name == "dsh_submit" and online:
            from app.worker_gateway.data_channel import request_key
            await self.bus.put_text(request_key(request_id), dsh_text, timeout + 30)
        if not online:
            return DispatchResult(command, False, False, "offline")
        delivered, reason = await self._handoff(command.id, owner_only=False)
        if not delivered and reason == "http_busy":
            return DispatchResult(command, False, False, None)
        return DispatchResult(command, delivered, False, None if delivered else reason)

    def _require_same_command(
        self,
        existing: WorkerCommand,
        worker_instance_id: str,
        name: str,
        payload: dict[str, Any],
        session_id: str | None,
    ) -> None:
        if (
            existing.worker_instance_id != worker_instance_id
            or existing.name != name
            or existing.payload != payload
            or existing.session_id != session_id
        ):
            raise WorkerGatewayError("duplicate_request", "request_id 已用于其他指令")

    async def dispatch_due(self, worker_instance_id: str, epoch: int) -> int:
        """只补投当前 epoch 上仍未 ACK 的指令。prompt 离开 unpublished 后不再发布。"""

        owner = await self.bus.get_owner(worker_instance_id)
        if owner is None or owner[0] != self.owner_id or owner[1] != epoch:
            return 0
        lease_before = utcnow() - timedelta(seconds=self.settings.worker_gateway_dispatch_lease_seconds)
        async with SessionLocal() as session:
            instance = await session.get(WorkerInstance, worker_instance_id)
            if instance is None or instance.epoch != epoch:
                return 0
            rows = (
                await session.scalars(
                    select(WorkerCommand).where(
                        WorkerCommand.worker_instance_id == worker_instance_id,
                        WorkerCommand.epoch == epoch,
                        WorkerCommand.status == "pending",
                        or_(
                            WorkerCommand.last_dispatched_at.is_(None),
                            WorkerCommand.last_dispatched_at <= lease_before,
                        ),
                    )
                )
            ).all()
            pending = [
                (row.id, row.name, row.delivery_state, row.payload if isinstance(row.payload, dict) else {})
                for row in rows
            ]
        delivered = 0
        for command_id, name, state, payload in pending:
            if self._side_effect(name, payload) and state != "unpublished":
                continue
            ok, _reason = await self._handoff(command_id, owner_only=True)
            if ok:
                delivered += 1
        return delivered

    async def _handoff(self, command_id: int, *, owner_only: bool) -> tuple[bool, str]:
        async with SessionLocal() as session:
            command = await session.get(WorkerCommand, command_id)
            if command is None or command.status != "pending":
                return False, "offline"
            if self._side_effect(command.name, command.payload) and command.delivery_state != "unpublished":
                return False, "needs_reconcile"
            instance = await session.get(WorkerInstance, command.worker_instance_id)
            if instance is None or instance.epoch != command.epoch:
                return False, "offline"
            owner = await self.bus.get_owner(command.worker_instance_id)
            if owner is None or owner[1] != command.epoch:
                return False, "offline"
            if owner_only and owner[0] != self.owner_id:
                return False, "offline"
            now = utcnow()
            lease_before = now - timedelta(seconds=self.settings.worker_gateway_dispatch_lease_seconds)
            previous_state = command.delivery_state
            attempts = command.delivery_attempts + 1
            worker_id = command.worker_instance_id
            epoch = command.epoch
            name = command.name
            reserved = False
            if name == "http" and previous_state == "unpublished":
                reserved = await reserve_http_slot(session, worker_id, epoch)
                if not reserved:
                    await session.rollback()
                    return False, "http_busy"
            current_instance = (
                select(WorkerInstance.id)
                .where(WorkerInstance.id == worker_id, WorkerInstance.epoch == epoch)
                .scalar_subquery()
            )
            claim = await session.execute(
                update(WorkerCommand)
                .where(
                    WorkerCommand.id == command.id,
                    WorkerCommand.epoch == epoch,
                    WorkerCommand.worker_instance_id == current_instance,
                    WorkerCommand.status == "pending",
                    WorkerCommand.delivery_state == previous_state,
                    or_(
                        WorkerCommand.last_dispatched_at.is_(None),
                        WorkerCommand.last_dispatched_at <= lease_before,
                    ),
                )
                .values(
                    delivery_state="uncertain",
                    delivery_attempts=WorkerCommand.delivery_attempts + 1,
                    last_dispatched_at=now,
                    dispatched_epoch=command.epoch,
                    updated_at=now,
                    **({"slot_held": True} if reserved else {}),
                )
            )
            if claim.rowcount != 1:
                await session.rollback()
                return False, "in_flight"
            frame = self._command_frame(command)
            await session.commit()
        try:
            published = await self.bus.publish_command(worker_id, frame)
        except Exception:
            logger.warning("worker command publish failed", exc_info=True)
            return False, "publish_unknown"
        if published:
            await self._mark_published(command_id, attempts, epoch)
            return True, ""
        await self._revert_definite(
            command_id, attempts, epoch, previous_state, worker_id=worker_id, release_slot=reserved
        )
        fresh_owner = await self.bus.get_owner(worker_id)
        if fresh_owner is None or fresh_owner[1] != epoch:
            return False, "offline"
        return False, "queue_full"

    async def _mark_published(self, command_id: int, attempts: int, epoch: int) -> None:
        async with SessionLocal() as session:
            await session.execute(
                update(WorkerCommand)
                .where(
                    WorkerCommand.id == command_id,
                    WorkerCommand.epoch == epoch,
                    WorkerCommand.delivery_attempts == attempts,
                )
                .values(delivery_state="published", updated_at=utcnow())
            )
            await session.commit()

    async def _revert_definite(
        self,
        command_id: int,
        attempts: int,
        epoch: int,
        previous_state: str,
        *,
        worker_id: str = "",
        release_slot: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "delivery_attempts": attempts - 1,
            "delivery_state": previous_state,
            "updated_at": utcnow(),
        }
        if previous_state == "unpublished":
            values["last_dispatched_at"] = None
            values["dispatched_epoch"] = None
            values["slot_held"] = False
        async with SessionLocal() as session:
            reverted = await session.execute(
                update(WorkerCommand)
                .where(
                    WorkerCommand.id == command_id,
                    WorkerCommand.epoch == epoch,
                    WorkerCommand.delivery_attempts == attempts,
                    WorkerCommand.status == "pending",
                )
                .values(**values)
            )
            if release_slot and worker_id and reverted.rowcount == 1:
                await release_http_slot(session, worker_id, epoch)
            await session.commit()

    @staticmethod
    def _publish_data(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if name == "http":
            return http_frame_data(payload)
        return payload

    @staticmethod
    def _side_effect(name: str, payload: dict[str, Any] | None) -> bool:
        return name in SIDE_EFFECT_COMMANDS or side_effect_http(name, payload)

    def _command_frame(self, command: WorkerCommand) -> dict[str, Any]:
        deadline = command.deadline_at or (
            utcnow() + timedelta(seconds=self.settings.worker_gateway_command_timeout_seconds)
        )
        message = envelope(
            request_id=command.request_id,
            worker_instance_id=command.worker_instance_id,
            environment_id=command.environment_id,
            session_id=command.session_id,
            message_type="command",
            payload={"name": command.name, "data": self._publish_data(command.name, command.payload or {})},
        )
        message.epoch = command.epoch
        message.expires_at = int(deadline.timestamp())
        return message.to_dict()

    async def apply_response(
        self,
        session: AsyncSession,
        *,
        worker_instance_id: str,
        epoch: int,
        message: WorkerMessage,
    ) -> bool:
        stage = message.payload.get("stage")
        now = utcnow()
        current_instance = (
            select(WorkerInstance.id)
            .where(WorkerInstance.id == worker_instance_id, WorkerInstance.epoch == epoch)
            .scalar_subquery()
        )
        where = (
            WorkerCommand.request_id == message.request_id,
            WorkerCommand.worker_instance_id == worker_instance_id,
            WorkerCommand.epoch == epoch,
            WorkerCommand.worker_instance_id == current_instance,
        )
        if stage == "ack":
            result = await session.execute(
                update(WorkerCommand)
                .where(*where, WorkerCommand.status == "pending")
                .values(status="accepted", updated_at=now)
            )
            await session.commit()
            return result.rowcount == 1
        if stage != "result":
            await session.rollback()
            return False
        error_code = message.payload.get("error_code")
        values: dict[str, Any] = {
            "finished_at": now,
            "updated_at": now,
        }
        if error_code:
            values["status"] = "failed"
            values["error_code"] = str(error_code)[:64]
        else:
            values["status"] = "succeeded"
            values["error_code"] = None
        result_body = message.payload.get("result")
        values["result"] = result_body if isinstance(result_body, dict) else {"value": result_body}
        released = await session.execute(
            update(WorkerCommand)
            .where(
                *where,
                WorkerCommand.status.in_(("pending", "accepted")),
                WorkerCommand.slot_held.is_(True),
            )
            .values(**values, slot_held=False)
        )
        if released.rowcount == 1:
            await release_http_slot(session, worker_instance_id, epoch)
            await session.commit()
            return True
        result = await session.execute(
            update(WorkerCommand)
            .where(*where, WorkerCommand.status.in_(("pending", "accepted")))
            .values(**values, slot_held=False)
        )
        await session.commit()
        return result.rowcount == 1

    async def epoch_is_current(self, session: AsyncSession, worker_instance_id: str, epoch: int) -> bool:
        instance = await session.get(WorkerInstance, worker_instance_id)
        return instance is not None and instance.epoch == epoch

    async def database_epoch(self, worker_instance_id: str) -> int | None:
        """Read the row epoch and drop a cached value that no longer matches."""

        async with SessionLocal() as session:
            instance = await session.get(WorkerInstance, worker_instance_id)
        epoch = None if instance is None else instance.epoch
        cached = self._db_epochs.get(worker_instance_id)
        if cached != epoch:
            self._db_epochs.pop(worker_instance_id, None)
            if epoch is not None:
                self._db_epochs[worker_instance_id] = epoch
        return epoch

    async def record_worker_event(self, auth: AuthOk, message: WorkerMessage) -> bool:
        payload = message.payload if isinstance(message.payload, dict) else {}
        directory = str(payload.get("directory") or "")
        if not directory or message.epoch != auth.epoch:
            return False
        if await self.database_epoch(auth.worker_instance_id) != message.epoch:
            return False
        from app.worker_gateway.events import EventLog

        await EventLog(self.bus, limit=self.settings.worker_gateway_event_queue_size).append(
            worker_id=auth.worker_instance_id,
            epoch=message.epoch,
            directory=directory,
            payload=payload,
        )
        return True

    async def sweep(self, *, now=None) -> int:
        current = now or utcnow()
        stale_after = timedelta(
            seconds=(
                self.settings.worker_gateway_ping_seconds
                + self.settings.worker_gateway_ping_timeout_seconds
            )
        )
        stale_before = current - stale_after
        stale_filter = (
            WorkerInstance.status == "online",
            or_(
                WorkerInstance.last_heartbeat_at.is_(None),
                WorkerInstance.last_heartbeat_at <= stale_before,
            ),
        )
        async with SessionLocal() as session:
            stale_rows = (
                await session.execute(select(WorkerInstance.id, WorkerInstance.epoch).where(*stale_filter))
            ).all()
            # 只下线这次读到的 epoch。重连若已切到新 epoch，不能被这次清扫改成离线。
            if stale_rows:
                pinned = or_(
                    *[
                        and_(
                            WorkerInstance.id == worker_id,
                            WorkerInstance.epoch == epoch,
                            *stale_filter,
                        )
                        for worker_id, epoch in stale_rows
                    ]
                )
                offline = await session.execute(
                    update(WorkerInstance).where(pinned).values(status="offline", updated_at=current)
                )
            else:
                offline = None
            expired_values = {
                "status": "expired",
                "error_code": "timeout",
                "finished_at": current,
                "updated_at": current,
                "slot_held": False,
            }
            due = (
                WorkerCommand.status.in_(("pending", "accepted")),
                WorkerCommand.deadline_at.is_not(None),
                WorkerCommand.deadline_at <= current,
            )
            held_rows = (
                await session.execute(
                    select(WorkerCommand.id, WorkerCommand.worker_instance_id, WorkerCommand.epoch).where(
                        *due, WorkerCommand.slot_held.is_(True)
                    )
                )
            ).all()
            released_slots: list[tuple[str, int]] = []
            for command_id, worker_id, epoch in held_rows:
                released = await session.execute(
                    held_slot_expire_statement(command_id, due, expired_values)
                )
                if released.rowcount != 1:
                    continue
                released_slots.append((worker_id, epoch))
                await release_http_slot(session, worker_id, epoch)
            expired = await session.execute(expire_due_commands_statement(due, expired_values))
            await session.commit()
            offlined: list[tuple[str, int]] = []
            if stale_rows:
                wanted = {(row[0], row[1]) for row in stale_rows}
                current_rows = (
                    await session.execute(
                        select(WorkerInstance.id, WorkerInstance.epoch, WorkerInstance.status).where(
                            WorkerInstance.id.in_([row[0] for row in stale_rows])
                        )
                    )
                ).all()
                offlined = [
                    (worker_id, epoch)
                    for worker_id, epoch, status in current_rows
                    if (worker_id, epoch) in wanted and status == "offline"
                ]
        for worker_id, epoch in offlined:
            self._close_local_socket(worker_id, epoch)
            await self.bus.clear_owner(worker_id, epoch, None)
        offline_count = 0 if offline is None else int(offline.rowcount or 0)
        return offline_count + len(released_slots) + int(expired.rowcount or 0)

    async def serve_socket(self, websocket: Any) -> None:
        await websocket.accept()
        auth: AuthOk | None = None
        queues: ConnectionQueues | None = None
        stop = asyncio.Event()
        watched: list[asyncio.Task[None]] = []
        try:
            auth = await self._read_auth(websocket)
            owned = await self.bus.set_owner(auth.worker_instance_id, self.owner_id, auth.epoch)
            if not owned:
                raise ConnectionError("epoch_stale")
            async with SessionLocal() as session:
                current = await self.epoch_is_current(session, auth.worker_instance_id, auth.epoch)
            if not current:
                await self.bus.clear_owner(auth.worker_instance_id, auth.epoch, self.owner_id)
                raise ConnectionError("epoch_stale")
            queues = await self.bus.bind(
                auth.worker_instance_id, epoch=auth.epoch, owner_id=self.owner_id
            )
            if queues is None:
                raise ConnectionError("epoch_stale")
            lease = _SocketLease(
                epoch=auth.epoch,
                websocket=websocket,
                inbound_events=deque(),
                event_limit=self.settings.worker_gateway_event_queue_size,
            )
            if not self._remember_socket(auth.worker_instance_id, lease):
                raise ConnectionError("epoch_stale")
            await websocket.send_text(dump_message(auth.ack))
            monitor = HeartbeatMonitor(self.settings.worker_gateway_heartbeat_misses)
            watched = [
                asyncio.create_task(self._send_loop(websocket, queues, auth, stop)),
                asyncio.create_task(self._ping_loop(auth, queues, monitor, stop, websocket)),
                asyncio.create_task(self._dispatch_loop(auth, stop, websocket)),
                asyncio.create_task(self._receive_loop(websocket, auth, lease, monitor, stop)),
            ]
            if queues.consumer is not None:
                watched.append(queues.consumer)
            done, _pending = await asyncio.wait(set(watched), return_when=asyncio.FIRST_COMPLETED)
            errors = [
                task.exception()
                for task in done
                if not task.cancelled() and task.exception() is not None
            ]
            if errors:
                raise errors[0]
            if queues.consumer is not None and queues.consumer.done():
                await self._try_close(websocket, 4000)
        except WorkerGatewayError as exc:
            await self._send_failure(websocket, exc)
            code = 1009 if exc.code == "frame_too_large" else 1008
            await self._try_close(websocket, code)
        except (WebSocketDisconnect, TimeoutError, ConnectionError):
            await self._try_close(websocket, 4000)
        except Exception:
            logger.exception("worker gateway session failed")
            await self._try_close(websocket, 1011)
        finally:
            stop.set()
            for task in watched:
                task.cancel()
            if watched:
                await asyncio.gather(*watched, return_exceptions=True)
            if auth is not None:
                if queues is not None:
                    await self.bus.unbind(auth.worker_instance_id, queues)
                await self.bus.clear_owner(auth.worker_instance_id, auth.epoch, self.owner_id)
                self._forget_socket(auth.worker_instance_id, auth.epoch)
                async with SessionLocal() as session:
                    await self.disconnect(session, auth.worker_instance_id, auth.epoch)

    async def aclose(self) -> None:
        for lease in list(self.sockets.values()):
            lease.superseded = True
            await self._try_close(lease.websocket, 1001)
        self.sockets.clear()
        await self.bus.aclose()

    async def _read_auth(self, websocket: Any) -> AuthOk:
        raw = await asyncio.wait_for(
            websocket.receive_text(),
            timeout=self.settings.worker_gateway_auth_timeout_seconds,
        )
        message = parse_message(raw, max_bytes=self.settings.worker_gateway_max_frame_bytes)
        token = message.payload.get("token")
        if not isinstance(token, str) or not token:
            raise WorkerGatewayError("unauthorized", "认证失败")
        async with SessionLocal() as session:
            return await self.authenticate(session, message, token)

    async def _receive_loop(
        self,
        websocket: Any,
        auth: AuthOk,
        lease: _SocketLease,
        monitor: HeartbeatMonitor,
        stop: asyncio.Event,
    ) -> None:
        while not stop.is_set():
            raw = await websocket.receive_text()
            try:
                message = parse_message(raw, max_bytes=self.settings.worker_gateway_max_frame_bytes)
            except WorkerGatewayError as exc:
                # One bad frame must not drop the Worker. The body can carry secrets.
                logger.warning("worker frame rejected code=%s", exc.code)
                continue
            if message.worker_instance_id != auth.worker_instance_id:
                raise WorkerGatewayError("invalid_message", "worker_instance_id 与连接不一致")
            if message.type == "heartbeat":
                await self._on_heartbeat(auth, message, monitor)
                if message.payload.get("kind") == "ping":
                    await self._queue_heartbeat(auth, message.request_id, "pong")
                continue
            if message.type == "event":
                lease.buffer_event(message.to_dict())
                if await self.record_worker_event(auth, message):
                    payload = message.payload if isinstance(message.payload, dict) else {}
                    await self.publish_event_control(auth, str(payload.get("directory") or ""))
                continue
            if message.type != "response":
                continue
            async with SessionLocal() as session:
                applied = await self.apply_response(
                    session,
                    worker_instance_id=auth.worker_instance_id,
                    epoch=auth.epoch,
                    message=message,
                )
            if applied:
                if message.payload.get("stage") == "result":
                    await self._route_response(message)
                continue
            async with SessionLocal() as session:
                current = await self.epoch_is_current(session, auth.worker_instance_id, auth.epoch)
            if not current:
                raise ConnectionError("epoch_stale")

    async def publish_event_control(self, auth: AuthOk, directory: str) -> str | None:
        from app.worker_gateway.events import EventLog

        action = await EventLog(
            self.bus, limit=self.settings.worker_gateway_event_queue_size
        ).control(worker_id=auth.worker_instance_id, epoch=auth.epoch, directory=directory)
        if action is None:
            return None
        queues = self.bus.bound.get(auth.worker_instance_id)
        if queues is None:
            return action
        message = envelope(
            request_id=f"evt-{uuid4().hex}",
            worker_instance_id=auth.worker_instance_id,
            environment_id=auth.environment_id,
            session_id=None,
            message_type="command",
            payload={"name": action, "data": {"directory": directory}},
        )
        message.epoch = auth.epoch
        message.expires_at = int(time.time()) + 30
        await queues.put_control(message.to_dict())
        return action

    async def _route_response(self, message: WorkerMessage) -> None:
        try:
            await self.bus.publish_response(message.request_id, message.to_dict())
        except Exception:
            logger.warning("worker response route failed", exc_info=True)

    async def _on_heartbeat(self, auth: AuthOk, message: WorkerMessage, monitor: HeartbeatMonitor) -> None:
        if message.payload.get("kind") == "pong":
            monitor.resolve(message.request_id)
        async with SessionLocal() as session:
            current = await self.note_heartbeat(session, auth.worker_instance_id, auth.epoch)
        if not current:
            raise ConnectionError("epoch_stale")

    async def _queue_heartbeat(self, auth: AuthOk, request_id: str, kind: str) -> None:
        queues = self.bus.bound.get(auth.worker_instance_id)
        if queues is None:
            return
        frame = self._heartbeat_frame(auth, request_id, kind)
        await queues.put_control(frame)

    async def _ping_loop(
        self,
        auth: AuthOk,
        queues: ConnectionQueues,
        monitor: HeartbeatMonitor,
        stop: asyncio.Event,
        websocket: Any,
    ) -> None:
        interval = self.settings.worker_gateway_ping_seconds
        timeout = self.settings.worker_gateway_ping_timeout_seconds
        while not stop.is_set():
            await asyncio.sleep(interval)
            if stop.is_set():
                return
            try:
                refreshed = await self.bus.touch_owner(
                    auth.worker_instance_id, self.owner_id, auth.epoch
                )
            except Exception:
                logger.warning("worker owner lease refresh failed", exc_info=True)
                refreshed = False
            if not refreshed:
                stop.set()
                await self._try_close(websocket, 4000)
                return
            request_id = f"hb_{uuid4().hex}"
            event = monitor.track(request_id)
            queued = await queues.put_control(self._heartbeat_frame(auth, request_id, "ping"))
            if not queued or not await self._wait_pong(event, timeout, stop):
                if monitor.timeout(request_id):
                    stop.set()
                    await self._try_close(websocket, 4001)
                    return

    async def _wait_pong(self, event: asyncio.Event, timeout: float, stop: asyncio.Event) -> bool:
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return not stop.is_set()

    def _heartbeat_frame(self, auth: AuthOk, request_id: str, kind: str) -> dict[str, Any]:
        message = envelope(
            request_id=request_id,
            worker_instance_id=auth.worker_instance_id,
            environment_id=auth.environment_id,
            session_id=None,
            message_type="heartbeat",
            payload={"kind": kind},
        )
        message.epoch = auth.epoch
        horizon = (
            self.settings.worker_gateway_ping_seconds
            + self.settings.worker_gateway_ping_timeout_seconds
        )
        message.expires_at = int(time.time()) + horizon
        return message.to_dict()

    async def _dispatch_loop(self, auth: AuthOk, stop: asyncio.Event, websocket: Any) -> None:
        interval = self.settings.worker_gateway_dispatch_lease_seconds
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), interval)
                return
            except TimeoutError:
                pass
            if stop.is_set():
                return
            try:
                if not await self.bus.touch_owner(auth.worker_instance_id, self.owner_id, auth.epoch):
                    stop.set()
                    await self._try_close(websocket, 4000)
                    return
                await self.dispatch_due(auth.worker_instance_id, auth.epoch)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker command dispatch failed")

    async def _send_loop(
        self, websocket: Any, queues: ConnectionQueues, auth: AuthOk, stop: asyncio.Event
    ) -> None:
        while not stop.is_set():
            waiting = asyncio.create_task(queues.wait_frame())
            halted = asyncio.create_task(stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {waiting, halted}, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                waiting.cancel()
                halted.cancel()
                await asyncio.gather(waiting, halted, return_exceptions=True)
                raise
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            if waiting not in done or waiting.cancelled():
                return
            frame = waiting.result()
            message = parse_message(
                dump_message(frame), max_bytes=self.settings.worker_gateway_max_frame_bytes
            )
            if not frame_acceptable(message, auth.epoch, time.time()):
                await queues.release_delivered(frame)
                continue
            if not await self.bus.touch_owner(auth.worker_instance_id, self.owner_id, auth.epoch):
                stop.set()
                await self._try_close(websocket, 4000)
                return
            await websocket.send_text(dump_message(message))
            await queues.release_delivered(frame)

    async def _supersede_connections(self, session: AsyncSession, worker_id: str, now, reason: str) -> None:
        await session.execute(
            update(WorkerConnection)
            .where(WorkerConnection.worker_instance_id == worker_id, WorkerConnection.state == "active")
            .values(state="superseded", close_reason=reason, closed_at=now)
        )

    def _remember_socket(self, worker_instance_id: str, lease: _SocketLease) -> bool:
        previous = self.sockets.get(worker_instance_id)
        if previous is not None and previous is not lease and previous.epoch >= lease.epoch:
            return False
        self.sockets[worker_instance_id] = lease
        if previous is not None and previous is not lease:
            previous.superseded = True
            asyncio.create_task(self._try_close(previous.websocket, 4000))
        return True

    def _forget_socket(self, worker_instance_id: str, epoch: int) -> None:
        current = self.sockets.get(worker_instance_id)
        if current is not None and current.epoch == epoch:
            del self.sockets[worker_instance_id]

    def _close_local_socket(self, worker_instance_id: str, epoch: int) -> None:
        lease = self.sockets.get(worker_instance_id)
        if lease is None or lease.epoch != epoch:
            return
        lease.superseded = True
        asyncio.create_task(self._try_close(lease.websocket, 4000))

    async def _send_failure(self, websocket: Any, exc: WorkerGatewayError) -> None:
        message = envelope(
            request_id="auth",
            worker_instance_id="",
            environment_id=0,
            session_id=None,
            message_type="response",
            payload={"ok": False, "error_code": exc.code},
        )
        try:
            await websocket.send_text(dump_message(message))
        except Exception:
            logger.debug("worker auth failure could not be sent", exc_info=True)

    async def _try_close(self, websocket: Any, code: int) -> None:
        try:
            await websocket.close(code=code)
        except Exception:
            logger.debug("worker socket close failed", exc_info=True)


_gateway: WorkerGateway | None = None


def get_gateway() -> WorkerGateway:
    global _gateway
    if _gateway is None:
        _gateway = WorkerGateway()
    return _gateway


async def sweep_worker_gateway() -> None:
    gateway = get_gateway()
    interval = gateway.settings.worker_gateway_ping_timeout_seconds
    while True:
        try:
            await gateway.sweep()
        except Exception:
            logger.exception("worker gateway sweep failed")
        await asyncio.sleep(interval)
