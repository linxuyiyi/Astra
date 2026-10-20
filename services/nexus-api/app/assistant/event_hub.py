"""平台助手事件总线。

OpenCode SSE → Event Mapper → NEXUS Assistant Event → 本 Hub → Frontend SSE。
Hub 为每个会话保留有限回放缓冲，前端断线重连后可通过 after_seq 补齐事件；
完整消息与卡片仍以数据库为准，刷新页面通过 messages 接口恢复。
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any


class AssistantEventHub:
    def __init__(self, replay_limit: int = 500):
        self._replay_limit = replay_limit
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._replay: dict[str, deque[tuple[int, dict[str, Any]]]] = {}
        self._seq: dict[str, int] = defaultdict(int)

    async def publish(self, conversation_id: str, event: dict[str, Any]) -> dict[str, Any]:
        self._seq[conversation_id] += 1
        envelope = {
            "seq": self._seq[conversation_id],
            "timestamp": datetime.now(UTC).isoformat(),
            "conversation_id": conversation_id,
            **event,
        }
        buffer = self._replay.setdefault(conversation_id, deque(maxlen=self._replay_limit))
        buffer.append((envelope["seq"], envelope))
        for queue in tuple(self._subscribers.get(conversation_id, ())):
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                # 慢消费者不应阻塞助手主流程；重连时可通过回放补齐。
                continue
        return envelope

    def replay(self, conversation_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        buffer = self._replay.get(conversation_id)
        if not buffer:
            return []
        return [envelope for seq, envelope in buffer if seq > after_seq]

    def current_seq(self, conversation_id: str) -> int:
        return self._seq.get(conversation_id, 0)

    def register(self, conversation_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._subscribers[conversation_id].add(queue)
        return queue

    def unregister(self, conversation_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._subscribers.get(conversation_id)
        if queues:
            queues.discard(queue)
            if not queues:
                self._subscribers.pop(conversation_id, None)

    async def subscribe(self, conversation_id: str) -> AsyncIterator[dict[str, Any]]:
        queue = self.register(conversation_id)
        try:
            while True:
                yield await queue.get()
        finally:
            self.unregister(conversation_id, queue)

    def drop_conversation(self, conversation_id: str) -> None:
        self._replay.pop(conversation_id, None)
        self._seq.pop(conversation_id, None)
        for queue in tuple(self._subscribers.get(conversation_id, ())):
            try:
                queue.put_nowait({"event": "assistant.conversation.deleted"})
            except asyncio.QueueFull:
                continue
        self._subscribers.pop(conversation_id, None)


assistant_event_hub = AssistantEventHub()
