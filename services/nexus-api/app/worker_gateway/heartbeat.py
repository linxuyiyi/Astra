"""心跳丢失计数。Gateway 与 Worker Agent 共用，不依赖数据库。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class HeartbeatMonitor:
    limit: int
    misses: int = 0
    pending: dict[str, asyncio.Event] = field(default_factory=dict)

    def track(self, request_id: str) -> asyncio.Event:
        event = asyncio.Event()
        self.pending[request_id] = event
        return event

    def resolve(self, request_id: str) -> bool:
        event = self.pending.pop(request_id, None)
        if event is None:
            return False
        event.set()
        self.misses = 0
        return True

    def timeout(self, request_id: str) -> bool:
        self.pending.pop(request_id, None)
        self.misses += 1
        return self.misses >= self.limit
