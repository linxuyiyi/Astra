"""平台助手模型请求的轻量限流器。

当前生产 unit 使用单 Uvicorn 进程，因此进程内滑动窗口能稳定
保护高成本模型入口。未来若扩展为多 Backend Worker，可在保持该接口
不变的前提下替换为 Redis 计数。
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import defaultdict, deque


class AssistantRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def consume(self, key: str, *, limit: int, window_seconds: int = 60) -> int | None:
        """记录一次请求；允许时返回 None，超限时返回建议重试秒数。"""

        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, math.ceil(window_seconds - (now - hits[0])))
            hits.append(now)
            return None

    def reset(self) -> None:
        self._hits.clear()


assistant_rate_limiter = AssistantRateLimiter()
