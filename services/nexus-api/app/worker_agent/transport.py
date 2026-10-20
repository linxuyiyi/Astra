"""把 WebSocket 库的异常收成连接错误。

websockets.ConnectionClosedError 不继承内置 ConnectionError。传输边界负责归一化，
连接循环只处理 WorkerTransportError，断线后继续退避重连。
"""

from __future__ import annotations

import json
from typing import Any


class WorkerTransportError(ConnectionError):
    """关闭、拒绝连接、超时和认证阶段的网络失败。"""


def _websocket_error(exc: BaseException) -> bool:
    try:
        from websockets.exceptions import WebSocketException
    except ImportError:
        return False
    return isinstance(exc, WebSocketException)


def translate_transport_error(exc: BaseException) -> WorkerTransportError:
    if isinstance(exc, WorkerTransportError):
        return exc
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) or _websocket_error(exc):
        return WorkerTransportError(exc.__class__.__name__)
    raise exc


class WebSocketTransport:
    def __init__(self, socket: Any) -> None:
        self.socket = socket

    async def send(self, message: dict[str, Any]) -> None:
        try:
            await self.socket.send(json.dumps(message, separators=(",", ":")))
        except Exception as exc:
            raise translate_transport_error(exc) from exc

    async def recv(self) -> dict[str, Any]:
        try:
            raw = await self.socket.recv()
        except Exception as exc:
            raise translate_transport_error(exc) from exc
        if isinstance(raw, bytes):
            raw = raw.decode()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise WorkerTransportError("invalid_json") from exc
        if not isinstance(data, dict):
            raise WorkerTransportError("invalid_json")
        return data

    async def close(self) -> None:
        try:
            await self.socket.close()
        except Exception as exc:
            raise translate_transport_error(exc) from exc


async def connect_websocket(url: str, *, max_size: int) -> WebSocketTransport:
    import websockets

    try:
        socket = await websockets.connect(url, max_size=max_size)
    except Exception as exc:
        raise translate_transport_error(exc) from exc
    return WebSocketTransport(socket)
