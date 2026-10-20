"""连接所有者与指令路由。

在线状态和 epoch 以数据库为准。这里只回答「哪个进程持有当前 epoch 的套接字」、
如何把已落库的指令送到那个进程，以及如何把结果交回等待中的调用方。
内存实现与 Redis 实现使用同一组方法。Redis 列表长度加上已取出未送达的数量
才是队列深度，容量检查和入队在同一段 Lua 里完成。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

SINGLE_PROCESS_WARNING = "仅单进程可用"

_SET_OWNER_LUA = """
local floor = tonumber(redis.call('GET', KEYS[2]) or '0')
local current = redis.call('GET', KEYS[1])
local new_owner = ARGV[1]
local new_epoch = tonumber(ARGV[2])
local live = 0
local owner = ''
local epoch_text = nil
if current then
  owner, epoch_text = string.match(current, '^(.-)|(%d+)$')
  live = tonumber(epoch_text) or 0
end
local high = floor
if live > high then
  high = live
end
if new_epoch < high then
  return 0
end
if current and live == new_epoch and owner ~= new_owner then
  return 0
end
if new_epoch > floor then
  redis.call('SET', KEYS[2], tostring(new_epoch))
end
redis.call('SET', KEYS[1], new_owner .. '|' .. ARGV[2], 'EX', ARGV[3])
return 1
"""

_PUSH_LUA = """
local current = redis.call('GET', KEYS[3])
if not current then
  return -1
end
local owner, epoch = string.match(current, '^(.-)|(%d+)$')
if epoch ~= ARGV[3] then
  return -1
end
local limit = tonumber(ARGV[1])
local held = tonumber(redis.call('GET', KEYS[2]) or '0')
if held + redis.call('LLEN', KEYS[1]) >= limit then
  return 0
end
redis.call('RPUSH', KEYS[1], ARGV[2])
local ttl = tonumber(ARGV[4])
redis.call('EXPIRE', KEYS[1], ttl)
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('EXPIRE', KEYS[2], ttl)
end
return 1
"""

_PULL_LUA = """
local current = redis.call('GET', KEYS[3])
if not current then
  return -1
end
local owner, epoch = string.match(current, '^(.-)|(%d+)$')
if epoch ~= ARGV[2] or owner ~= ARGV[3] then
  return -1
end
local item = redis.call('LPOP', KEYS[1])
if not item then
  return 0
end
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[1])
return item
"""

_RESTORE_LUA = """
redis.call('LPUSH', KEYS[1], ARGV[1])
local held = tonumber(redis.call('GET', KEYS[2]) or '0')
if held > 0 then
  redis.call('DECR', KEYS[2])
end
local ttl = tonumber(ARGV[2])
redis.call('EXPIRE', KEYS[1], ttl)
if redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('EXPIRE', KEYS[2], ttl)
end
return 1
"""

_RELEASE_LUA = """
local n = tonumber(ARGV[1])
local held = tonumber(redis.call('GET', KEYS[1]) or '0')
if held <= 0 or n <= 0 then
  return 0
end
if n >= held then
  redis.call('DEL', KEYS[1])
  return held
end
redis.call('DECRBY', KEYS[1], n)
return n
"""

_CLEAR_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
  return 0
end
local owner, epoch = string.match(current, '^(.-)|(%d+)$')
if epoch ~= ARGV[1] then
  return 0
end
if ARGV[2] ~= '' and owner ~= ARGV[2] then
  return 0
end
redis.call('DEL', KEYS[1])
return 1
"""

_TOUCH_LUA = """
local current = redis.call('GET', KEYS[1])
if not current then
  return 0
end
local owner, epoch = string.match(current, '^(.-)|(%d+)$')
if owner ~= ARGV[1] or epoch ~= ARGV[2] then
  return 0
end
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

_RESULT_LUA = """
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""


def resolve_worker_gateway_bus(requested: str, redis_url: str) -> tuple[str, str | None]:
    """auto 在 Redis URL 非空时用 redis，否则用 memory。memory / redis 显式覆盖。"""

    choice = (requested or "auto").strip().lower()
    if choice not in {"auto", "memory", "redis"}:
        choice = "auto"
    if choice == "redis":
        return "redis", None
    if choice == "memory" or not (redis_url or "").strip():
        return "memory", SINGLE_PROCESS_WARNING
    return "redis", None


def _ttl_seconds(seconds: float) -> int:
    return max(1, int(seconds))


def frame_epoch(frame: dict[str, Any]) -> int | None:
    value = frame.get("epoch")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def frame_is_current(frame: dict[str, Any], now: float | None = None) -> bool:
    epoch = frame_epoch(frame)
    expires_at = frame.get("expires_at")
    if epoch is None or isinstance(expires_at, bool) or not isinstance(expires_at, int):
        return False
    return expires_at > int(time.time() if now is None else now)


class ConnectionQueues:
    """控制帧优先于事件帧。事件队列满时丢弃事件并计数，不阻塞控制帧。"""

    def __init__(
        self,
        control_limit: int,
        event_limit: int,
        on_delivered=None,
    ) -> None:
        self.control: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=control_limit)
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=event_limit)
        self.dropped_events = 0
        self.epoch: int | None = None
        self.owner_id: str | None = None
        self.bus_pending = {"control": 0, "event": 0}
        self.consumer: asyncio.Task[None] | None = None
        self._signal = asyncio.Event()
        self._lock = asyncio.Lock()
        self._on_delivered = on_delivered

    def _count_bus(self, kind: str, frame: dict[str, Any]) -> None:
        if frame.get("_bus"):
            self.bus_pending[kind] += 1

    async def put_control(self, frame: dict[str, Any]) -> bool:
        async with self._lock:
            if self.control.full():
                return False
            self.control.put_nowait(frame)
            self._count_bus("control", frame)
            self._signal.set()
            return True

    async def put_event(self, frame: dict[str, Any]) -> bool:
        async with self._lock:
            if self.events.full():
                self.dropped_events += 1
                return False
            self.events.put_nowait(frame)
            self._count_bus("event", frame)
            self._signal.set()
            return True

    def _pop_locked(self) -> tuple[str, dict[str, Any]] | None:
        if not self.control.empty():
            return "control", self.control.get_nowait()
        if not self.events.empty():
            return "event", self.events.get_nowait()
        return None

    async def wait_frame(self) -> dict[str, Any]:
        while True:
            async with self._lock:
                popped = self._pop_locked()
                if popped is not None:
                    if self.control.empty() and self.events.empty():
                        self._signal.clear()
                    kind, frame = popped
                    if frame.get("_bus"):
                        self.bus_pending[kind] = max(0, self.bus_pending[kind] - 1)
                else:
                    kind, frame = None, None
                    self._signal.clear()
            if kind is not None and frame is not None:
                return frame
            await self._signal.wait()

    async def release_delivered(self, frame: dict[str, Any]) -> None:
        """套接字发送成功后再归还 Redis 容量。本地帧不占这份额度。"""

        if self._on_delivered is None or not frame.get("_bus"):
            return
        kind = frame.get("_bus_kind")
        if kind not in {"control", "event"}:
            return
        await self._on_delivered(kind, frame)


class MemoryRouteBus:
    mode = "memory"

    def __init__(
        self,
        *,
        control_limit: int = 64,
        event_limit: int = 64,
        owner_ttl_seconds: float = 60,
        result_ttl_seconds: float = 60,
    ) -> None:
        self.control_limit = control_limit
        self.event_limit = event_limit
        self.owner_ttl_seconds = owner_ttl_seconds
        self.result_ttl_seconds = result_ttl_seconds
        self.owners: dict[str, tuple[str, int, float]] = {}
        self.epoch_floor: dict[str, int] = {}
        self.bound: dict[str, ConnectionQueues] = {}
        self._results: dict[str, tuple[float, dict[str, Any]]] = {}
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._texts: dict[str, tuple[float, str]] = {}
        self._text_waiters: dict[str, list[asyncio.Event]] = {}

    def _remember_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> None:
        self.owners[worker_instance_id] = (
            owner_id,
            epoch,
            time.monotonic() + self.owner_ttl_seconds,
        )

    def _live_owner(self, worker_instance_id: str) -> tuple[str, int, float] | None:
        current = self.owners.get(worker_instance_id)
        if current is None:
            return None
        if current[2] <= time.monotonic():
            self.epoch_floor[worker_instance_id] = max(
                self.epoch_floor.get(worker_instance_id, 0), current[1]
            )
            del self.owners[worker_instance_id]
            return None
        return current

    async def set_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> bool:
        if "|" in owner_id:
            raise ValueError("owner_id 不能包含 |")
        current = self._live_owner(worker_instance_id)
        live = current[1] if current is not None else 0
        floor = self.epoch_floor.get(worker_instance_id, 0)
        if epoch < max(floor, live):
            return False
        if current is not None and current[1] == epoch and current[0] != owner_id:
            return False
        if epoch > floor:
            self.epoch_floor[worker_instance_id] = epoch
        self._remember_owner(worker_instance_id, owner_id, epoch)
        return True

    async def get_owner(self, worker_instance_id: str) -> tuple[str, int] | None:
        current = self._live_owner(worker_instance_id)
        if current is None:
            return None
        return current[0], current[1]

    async def touch_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> bool:
        current = self._live_owner(worker_instance_id)
        if current is None or current[0] != owner_id or current[1] != epoch:
            return False
        self._remember_owner(worker_instance_id, owner_id, epoch)
        return True

    async def clear_owner(
        self, worker_instance_id: str, epoch: int, owner_id: str | None
    ) -> bool:
        current = self._live_owner(worker_instance_id)
        if current is None or current[1] != epoch:
            return False
        if owner_id is not None and current[0] != owner_id:
            return False
        del self.owners[worker_instance_id]
        return True

    async def bind(
        self, worker_instance_id: str, *, epoch: int, owner_id: str
    ) -> ConnectionQueues | None:
        current = self._live_owner(worker_instance_id)
        if current is None or current[0] != owner_id or current[1] != epoch:
            return None
        previous = self.bound.get(worker_instance_id)
        if previous is not None and previous.epoch is not None and previous.epoch > epoch:
            return None
        queues = ConnectionQueues(self.control_limit, self.event_limit)
        queues.epoch = epoch
        queues.owner_id = owner_id
        self.bound[worker_instance_id] = queues
        return queues

    async def unbind(self, worker_instance_id: str, queues: ConnectionQueues) -> None:
        if self.bound.get(worker_instance_id) is queues:
            del self.bound[worker_instance_id]

    def _queues_for(self, worker_instance_id: str, frame: dict[str, Any]) -> ConnectionQueues | None:
        epoch = frame_epoch(frame)
        if epoch is None or not frame_is_current(frame):
            return None
        current = self._live_owner(worker_instance_id)
        if current is None or current[1] != epoch:
            return None
        queues = self.bound.get(worker_instance_id)
        if queues is None or queues.epoch != epoch:
            return None
        return queues

    async def publish_command(self, worker_instance_id: str, frame: dict[str, Any]) -> bool:
        queues = self._queues_for(worker_instance_id, frame)
        if queues is None:
            return False
        return await queues.put_control(frame)

    async def publish_event(self, worker_instance_id: str, frame: dict[str, Any]) -> bool:
        queues = self._queues_for(worker_instance_id, frame)
        if queues is None:
            return False
        return await queues.put_event(frame)

    async def publish_response(self, request_id: str, frame: dict[str, Any]) -> None:
        waiter = self._waiters.pop(request_id, None)
        if waiter is not None and not waiter.done():
            waiter.set_result(frame)
            return
        self._results[request_id] = (time.monotonic() + self.result_ttl_seconds, frame)

    async def wait_response(self, request_id: str, timeout: float) -> dict[str, Any] | None:
        self._drop_expired_results()
        stored = self._results.pop(request_id, None)
        if stored is not None:
            return stored[1]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            self._waiters.pop(request_id, None)
            return None

    def _drop_expired_results(self) -> None:
        now = time.monotonic()
        expired = [key for key, (deadline, _) in self._results.items() if deadline <= now]
        for key in expired:
            del self._results[key]

    async def put_text(self, key: str, value: str, ttl_seconds: float) -> None:
        self._texts[key] = (time.monotonic() + ttl_seconds, value)
        for waiter in self._text_waiters.get(key, ()):
            waiter.set()

    async def wait_text(self, key: str, timeout: float) -> None:
        waiter = asyncio.Event()
        self._text_waiters.setdefault(key, []).append(waiter)
        try:
            await asyncio.wait_for(waiter.wait(), timeout)
        except TimeoutError:
            return
        finally:
            waiters = self._text_waiters.get(key, [])
            if waiter in waiters:
                waiters.remove(waiter)
            if not waiters:
                self._text_waiters.pop(key, None)

    async def get_text(self, key: str) -> str | None:
        item = self._texts.get(key)
        if item is None:
            return None
        if item[0] <= time.monotonic():
            self._texts.pop(key, None)
            return None
        return item[1]

    async def delete_text(self, key: str) -> None:
        self._texts.pop(key, None)

    async def texts_with_prefix(self, prefix: str) -> list[tuple[str, str]]:
        now = time.monotonic()
        found: list[tuple[str, str]] = []
        for key, (deadline, raw) in list(self._texts.items()):
            if key.startswith(prefix) and deadline > now:
                found.append((key, raw))
        return found

    async def aclose(self) -> None:
        return None


class RedisRouteBus:
    """跨进程路由。租约、有界队列和响应都在 Redis，调用方不另存一份在线表。"""

    mode = "redis"

    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "nexus:worker-gateway",
        control_limit: int = 64,
        event_limit: int = 64,
        owner_ttl_seconds: float = 60,
        result_ttl_seconds: float = 60,
    ) -> None:
        self.redis_url = redis_url
        self.prefix = prefix
        self.control_limit = control_limit
        self.event_limit = event_limit
        self.owner_ttl_seconds = owner_ttl_seconds
        self.result_ttl_seconds = result_ttl_seconds
        self._client: Any | None = None
        self.bound: dict[str, ConnectionQueues] = {}
        self._pulls: dict[int, tuple[asyncio.Event, asyncio.Task[None]]] = {}
        self._bind_lock = asyncio.Lock()

    def _connection(self) -> Any:
        if self._client is None:
            from redis.asyncio import Redis

            self._client = Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def _owner_key(self, worker_instance_id: str) -> str:
        return f"{self.prefix}:owner:{worker_instance_id}"

    def _floor_key(self, worker_instance_id: str) -> str:
        return f"{self.prefix}:epoch:{worker_instance_id}"

    def _queue_ttl(self) -> int:
        return max(60, _ttl_seconds(self.owner_ttl_seconds) * 2)

    def _control_key(self, worker_instance_id: str, epoch: int) -> str:
        return f"{self.prefix}:control:{worker_instance_id}:{epoch}"

    def _event_key(self, worker_instance_id: str, epoch: int) -> str:
        return f"{self.prefix}:events:{worker_instance_id}:{epoch}"

    def _held_key(self, worker_instance_id: str, kind: str, epoch: int) -> str:
        return f"{self.prefix}:{kind}-held:{worker_instance_id}:{epoch}"

    def _result_key(self, request_id: str) -> str:
        return f"{self.prefix}:result:{request_id}"

    def _text_key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def _notify_key(self, key: str) -> str:
        return f"{self.prefix}:notify:{key}"

    async def put_text(self, key: str, value: str, ttl_seconds: float) -> None:
        client = self._connection()
        ttl = max(1, int(ttl_seconds))
        notify = self._notify_key(key)
        async with client.pipeline(transaction=True) as pipe:
            pipe.set(self._text_key(key), value, ex=ttl)
            pipe.rpush(notify, "1")
            pipe.ltrim(notify, -8, -1)
            pipe.expire(notify, max(2, ttl))
            await pipe.execute()

    async def wait_text(self, key: str, timeout: float) -> None:
        await self._connection().blpop(self._notify_key(key), timeout=max(1, int(timeout)))

    async def get_text(self, key: str) -> str | None:
        return await self._connection().get(self._text_key(key))

    async def delete_text(self, key: str) -> None:
        await self._connection().delete(self._text_key(key))

    async def texts_with_prefix(self, prefix: str) -> list[tuple[str, str]]:
        client = self._connection()
        pattern = f"{self._text_key(prefix)}*"
        found: list[tuple[str, str]] = []
        async for key in client.scan_iter(match=pattern, count=100):
            raw = await client.get(key)
            if raw is None:
                continue
            logical = key[len(self.prefix) + 1 :]
            found.append((logical, raw))
        return found

    async def set_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> bool:
        if "|" in owner_id:
            raise ValueError("owner_id 不能包含 |")
        result = await self._connection().eval(
            _SET_OWNER_LUA,
            2,
            self._owner_key(worker_instance_id),
            self._floor_key(worker_instance_id),
            owner_id,
            str(epoch),
            str(_ttl_seconds(self.owner_ttl_seconds)),
        )
        return int(result) == 1

    async def get_owner(self, worker_instance_id: str) -> tuple[str, int] | None:
        raw = await self._connection().get(self._owner_key(worker_instance_id))
        if not raw:
            return None
        owner, _, epoch = raw.rpartition("|")
        return owner, int(epoch)

    async def touch_owner(self, worker_instance_id: str, owner_id: str, epoch: int) -> bool:
        result = await self._connection().eval(
            _TOUCH_LUA,
            1,
            self._owner_key(worker_instance_id),
            owner_id,
            str(epoch),
            str(_ttl_seconds(self.owner_ttl_seconds)),
        )
        return int(result) == 1

    async def clear_owner(
        self, worker_instance_id: str, epoch: int, owner_id: str | None
    ) -> bool:
        result = await self._connection().eval(
            _CLEAR_LUA,
            1,
            self._owner_key(worker_instance_id),
            str(epoch),
            owner_id or "",
        )
        return int(result) == 1

    async def bind(
        self, worker_instance_id: str, *, epoch: int, owner_id: str
    ) -> ConnectionQueues | None:
        async with self._bind_lock:
            current = await self.get_owner(worker_instance_id)
            if current != (owner_id, epoch):
                return None
            previous = self.bound.get(worker_instance_id)
            if previous is not None and previous.epoch is not None and previous.epoch > epoch:
                return None
            queues = self._start_pull(worker_instance_id, epoch, owner_id)
            return queues

    def _start_pull(self, worker_instance_id: str, epoch: int, owner_id: str) -> ConnectionQueues:
        queues = ConnectionQueues(
            self.control_limit,
            self.event_limit,
            on_delivered=lambda kind, frame: self._release_delivered(
                worker_instance_id, kind, epoch, frame
            ),
        )
        queues.epoch = epoch
        queues.owner_id = owner_id
        self.bound[worker_instance_id] = queues
        stop = asyncio.Event()
        task = asyncio.create_task(
            self._pull(worker_instance_id, queues, stop, epoch, owner_id)
        )
        queues.consumer = task
        self._pulls[id(queues)] = (stop, task)
        return queues

    async def unbind(self, worker_instance_id: str, queues: ConnectionQueues) -> None:
        async with self._bind_lock:
            if self.bound.get(worker_instance_id) is queues:
                del self.bound[worker_instance_id]
        pull = self._pulls.pop(id(queues), None)
        if pull is not None:
            pull[0].set()
            pull[1].cancel()
            await asyncio.gather(pull[1], return_exceptions=True)
        epoch = queues.epoch
        if epoch is None:
            return
        await self._connection().delete(
            self._control_key(worker_instance_id, epoch),
            self._event_key(worker_instance_id, epoch),
            self._held_key(worker_instance_id, "control", epoch),
            self._held_key(worker_instance_id, "event", epoch),
        )

    async def publish_command(self, worker_instance_id: str, frame: dict[str, Any]) -> bool:
        epoch = frame_epoch(frame)
        if epoch is None or not frame_is_current(frame):
            return False
        return await self._push(
            self._control_key(worker_instance_id, epoch),
            self._held_key(worker_instance_id, "control", epoch),
            self._owner_key(worker_instance_id),
            frame,
            self.control_limit,
            epoch,
        )

    async def publish_event(self, worker_instance_id: str, frame: dict[str, Any]) -> bool:
        epoch = frame_epoch(frame)
        if epoch is None or not frame_is_current(frame):
            return False
        return await self._push(
            self._event_key(worker_instance_id, epoch),
            self._held_key(worker_instance_id, "event", epoch),
            self._owner_key(worker_instance_id),
            frame,
            self.event_limit,
            epoch,
        )

    async def publish_response(self, request_id: str, frame: dict[str, Any]) -> None:
        await self._connection().eval(
            _RESULT_LUA,
            1,
            self._result_key(request_id),
            json.dumps(frame),
            str(_ttl_seconds(self.result_ttl_seconds)),
        )

    async def wait_response(self, request_id: str, timeout: float) -> dict[str, Any] | None:
        client = self._connection()
        key = self._result_key(request_id)
        item = await client.lpop(key)
        if item:
            return json.loads(item)
        if timeout <= 0:
            return None
        popped = await client.blpop(key, timeout=timeout)
        if not popped:
            return None
        return json.loads(popped[1])

    async def _push(
        self,
        list_key: str,
        held_key: str,
        owner_key: str,
        frame: dict[str, Any],
        limit: int,
        epoch: int,
    ) -> bool:
        result = await self._connection().eval(
            _PUSH_LUA,
            3,
            list_key,
            held_key,
            owner_key,
            str(limit),
            json.dumps(frame),
            str(epoch),
            str(self._queue_ttl()),
        )
        return int(result) == 1

    async def _release_delivered(
        self, worker_instance_id: str, kind: str, epoch: int, frame: dict[str, Any]
    ) -> None:
        if not frame.get("_bus"):
            return
        await self._release(worker_instance_id, kind, epoch, 1)

    async def _release(self, worker_instance_id: str, kind: str, epoch: int, count: int) -> None:
        if count <= 0:
            return
        await self._connection().eval(
            _RELEASE_LUA, 1, self._held_key(worker_instance_id, kind, epoch), str(count)
        )

    async def _pull(
        self,
        worker_instance_id: str,
        queues: ConnectionQueues,
        stop: asyncio.Event,
        epoch: int,
        owner_id: str,
    ) -> None:
        client = self._connection()
        owner_key = self._owner_key(worker_instance_id)
        control_key = self._control_key(worker_instance_id, epoch)
        event_key = self._event_key(worker_instance_id, epoch)
        control_held = self._held_key(worker_instance_id, "control", epoch)
        event_held = self._held_key(worker_instance_id, "event", epoch)
        ttl = str(_ttl_seconds(self.owner_ttl_seconds))
        while not stop.is_set():
            if not await self.touch_owner(worker_instance_id, owner_id, epoch):
                return
            moved = await self._transfer(
                client, queues, control_key, control_held, owner_key, ttl, epoch, owner_id, "control"
            )
            if moved is None:
                return
            if queues.control.qsize() == 0:
                event_moved = await self._transfer(
                    client, queues, event_key, event_held, owner_key, ttl, epoch, owner_id, "event"
                )
                if event_moved is None:
                    return
                moved = event_moved or moved
            if queues.control.qsize():
                await client.expire(control_held, int(ttl))
            if queues.events.qsize():
                await client.expire(event_held, int(ttl))
            if not moved:
                await asyncio.sleep(0.05)

    async def _transfer(
        self,
        client,
        queues: ConnectionQueues,
        list_key: str,
        held_key: str,
        owner_key: str,
        ttl: str,
        epoch: int,
        owner_id: str,
        kind: str,
    ) -> bool | None:
        if kind == "control" and queues.control.full():
            return False
        if kind == "event" and queues.events.full():
            return False
        item = await client.eval(
            _PULL_LUA, 3, list_key, held_key, owner_key, ttl, str(epoch), owner_id
        )
        if item == -1:
            return None
        if item == 0 or item is None:
            return False
        payload = json.loads(item)
        payload["_bus"] = True
        payload["_bus_kind"] = kind
        accepted = await (
            queues.put_control(payload) if kind == "control" else queues.put_event(payload)
        )
        if accepted:
            return True
        if kind == "control":
            await client.eval(_RESTORE_LUA, 2, list_key, held_key, item, str(self._queue_ttl()))
        else:
            await self._release_key(held_key, 1)
        return False

    async def _release_key(self, held_key: str, count: int) -> None:
        await self._connection().eval(_RELEASE_LUA, 1, held_key, str(count))

    async def delete_prefix(self) -> None:
        client = self._connection()
        keys = [key async for key in client.scan_iter(match=f"{self.prefix}:*")]
        if keys:
            await client.delete(*keys)

    async def aclose(self) -> None:
        pulls = list(self._pulls.values())
        self._pulls.clear()
        for stop, task in pulls:
            stop.set()
            task.cancel()
        if pulls:
            await asyncio.gather(*(task for _, task in pulls), return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_route_bus(
    requested: str,
    redis_url: str,
    *,
    control_limit: int,
    event_limit: int,
) -> tuple[MemoryRouteBus | RedisRouteBus, str, str | None]:
    mode, warning = resolve_worker_gateway_bus(requested, redis_url)
    if mode == "redis":
        return RedisRouteBus(
            redis_url, control_limit=control_limit, event_limit=event_limit
        ), mode, warning
    return MemoryRouteBus(control_limit=control_limit, event_limit=event_limit), mode, warning
