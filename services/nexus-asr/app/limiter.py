"""Small in-process sliding-window rate limiter.

Single-process by design: the service runs one uvicorn worker so the model is
loaded once. Nginx also applies a per-IP limit; this is defence in depth.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float = 60.0) -> None:
        self.limit = max(0, limit)
        self.window = window_seconds
        self._events: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> float | None:
        """Return None when allowed, otherwise seconds until the next slot."""
        if self.limit <= 0:
            return None
        now = time.monotonic()
        cutoff = now - self.window
        async with self._lock:
            queue = self._events.setdefault(key, deque())
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if len(queue) >= self.limit:
                return max(0.1, queue[0] + self.window - now)
            queue.append(now)
            if len(self._events) > 2048:
                stale = [k for k, q in self._events.items() if not q or q[-1] <= cutoff]
                for k in stale:
                    self._events.pop(k, None)
            return None
