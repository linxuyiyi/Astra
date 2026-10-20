"""Per-directory event log, cursor leases, and pause/resume/stop."""

from __future__ import annotations

import json
import time
from typing import Any

LEASE_SECONDS = 30


class EventLog:
    def __init__(self, bus, *, limit: int, lease_seconds: float = LEASE_SECONDS) -> None:
        self.bus = bus
        self.limit = limit
        self.lease_seconds = lease_seconds

    def _key(self, worker_id: str, epoch: int, directory: str) -> str:
        return f"eventlog:{worker_id}:{epoch}:{directory}"

    def _cursor_key(self, worker_id: str, epoch: int, directory: str, consumer: str) -> str:
        return f"eventcursor:{worker_id}:{epoch}:{directory}:{consumer}"

    async def _load(self, key: str) -> dict[str, Any]:
        raw = await self.bus.get_text(key)
        if not raw:
            return {"events": [], "paused": False, "stopped": False, "gap_refresh": False}
        parsed = json.loads(raw)
        parsed.setdefault("events", [])
        return parsed

    async def _save(self, key: str, state: dict[str, Any]) -> None:
        await self.bus.put_text(key, json.dumps(state), 3600)

    async def append(
        self, *, worker_id: str, epoch: int, directory: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        key = self._key(worker_id, epoch, directory)
        state = await self._load(key)
        events = state["events"]
        last = events[-1]["seq"] if events else 0
        seq = payload.get("seq")
        dropped = bool(payload.get("dropped"))
        if not isinstance(seq, int) or seq <= last or (not dropped and seq != last + 1):
            state["gap_refresh"] = True
            marker = seq if isinstance(seq, int) and seq > last else last
            state["events"] = self._retain(
                events + [{"seq": marker, "gap": True, "directory": directory, "from": last, "to": seq}],
                directory,
            )
            await self._save(key, state)
            return {"action": "gap", "from": last, "to": seq}
        if dropped:
            state["gap_refresh"] = True
            state["events"] = self._retain(
                events + [{"seq": seq, "gap": True, "directory": directory, "reason": payload.get("reason")}],
                directory,
            )
            await self._save(key, state)
            return {"action": "gap", "from": last, "to": seq}
        state["events"] = self._retain(
            events + [{"seq": seq, "directory": directory, "event": payload.get("event")}],
            directory,
        )
        await self._save(key, state)
        return {"action": "store", "seq": seq}

    def _retain(self, events: list[dict[str, Any]], directory: str) -> list[dict[str, Any]]:
        """Keep the newest events. A trim leaves a gap a lagging cursor can observe."""

        if len(events) <= self.limit:
            return events
        dropped = events[: len(events) - self.limit]
        kept = list(events[len(events) - self.limit :])
        start = dropped[0].get("from") if dropped[0].get("gap") else dropped[0].get("seq")
        end = kept[0].get("seq")
        gap = {
            "seq": end,
            "gap": True,
            "directory": directory,
            "from": start,
            "to": end,
            "reason": "log_trimmed",
        }
        return [gap, *kept[1:]]

    async def touch_cursor(
        self, *, worker_id: str, epoch: int, directory: str, consumer: str, seq: int
    ) -> None:
        await self.bus.put_text(
            self._cursor_key(worker_id, epoch, directory, consumer),
            json.dumps({"seq": seq, "expires_at": time.time() + self.lease_seconds}),
            self.lease_seconds,
        )

    async def control(self, *, worker_id: str, epoch: int, directory: str) -> str | None:
        state = await self._load(self._key(worker_id, epoch, directory))
        events = state["events"]
        if not events:
            return None
        head = events[-1]["seq"]
        live = []
        prefix = f"eventcursor:{worker_id}:{epoch}:{directory}:"
        now = time.time()
        for _key, raw in await self.bus.texts_with_prefix(prefix):
            item = json.loads(raw)
            if item.get("expires_at", 0) > now:
                live.append(int(item["seq"]))
        if not live:
            await self.bus.delete_text(self._key(worker_id, epoch, directory))
            return "stop_events"
        lag = head - min(live)
        if lag >= self.limit and not state.get("paused"):
            state["paused"] = True
            state["stopped"] = False
            await self._save(self._key(worker_id, epoch, directory), state)
            return "pause_events"
        if lag < self.limit and state.get("paused"):
            state["paused"] = False
            await self._save(self._key(worker_id, epoch, directory), state)
            return "resume_events"
        return None

    async def begin_refresh(self, *, worker_id: str, epoch: int, directory: str) -> bool:
        """True when this caller should run refresh. Later gaps join the same one."""

        key = self._key(worker_id, epoch, directory)
        state = await self._load(key)
        if state.get("refreshing"):
            state["gap_refresh"] = True
            await self._save(key, state)
            return False
        state["refreshing"] = True
        state["gap_refresh"] = False
        await self._save(key, state)
        return True

    async def finish_refresh(self, *, worker_id: str, epoch: int, directory: str) -> bool:
        key = self._key(worker_id, epoch, directory)
        state = await self._load(key)
        again = bool(state.get("gap_refresh"))
        state["refreshing"] = False
        state["gap_refresh"] = False
        await self._save(key, state)
        return again

    async def wait_append(self, *, worker_id: str, epoch: int, directory: str, timeout: float = 1) -> None:
        await self.bus.wait_text(self._key(worker_id, epoch, directory), timeout)
