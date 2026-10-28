"""日程资源准备必须发生在官方推理前，失败走官方装配错误。"""

import asyncio
from types import SimpleNamespace

import httpx
from agentscope.app.storage import ScheduleOrigin
from agentscope.event import UserInterruptEvent
from schedule_resources import (
    install_schedule_preparation,
    schedule_run,
    schedule_toolkit,
)


def test_prepare_before_run_failure_and_interrupt(monkeypatch):
    calls = []
    sid = "session-schedule"
    user = "nexus-schedule-" + "a" * 64

    class Storage:
        async def get_session(self, *args):
            return SimpleNamespace(origin=ScheduleOrigin(schedule_id="schedule-1"))

    class Bus:
        def acquire_lock(self, *args, **kwargs):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def lock():
                yield

            return lock()

    class Chat:
        _storage = Storage()
        _message_bus = Bus()

        async def run(self, *args):
            calls.append(("run", schedule_run.get()))

    async def request(_self, method, url, **kwargs):
        calls.append(("prepare", kwargs["json"]))
        return httpx.Response(
            200, json={"ok": True}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", request)
    install_schedule_preparation(Chat, "http://nexus")

    async def scenario():
        await Chat().run(user, sid, "agent-1")
        assert [c[0] for c in calls] == ["prepare", "run"]
        assert calls[1][1].error == ""
        assert schedule_run.get() is None
        calls.clear()

        async def unavailable(*args, **kwargs):
            raise httpx.ConnectError("secret upstream address")

        monkeypatch.setattr(httpx.AsyncClient, "request", unavailable)
        await Chat().run(user, sid, "agent-1")
        assert calls[0][0] == "run"
        assert calls[0][1].error == "日程资源准备失败，请检查员工资源与服务连接"
        calls.clear()
        await Chat().run(user, sid, "agent-1", UserInterruptEvent(reply_id="reply-1"))
        assert calls[0][1].interrupt
        assert calls[0][1].error == ""
        assert schedule_run.get() is None

        async def unexpected_lookup(*args):
            raise AssertionError("普通聊天不应额外读取日程 Session")
        monkeypatch.setattr(Chat._storage, "get_session", unexpected_lookup)
        calls.clear()
        await Chat().run("nexus", sid, "agent-1")
        assert calls == [("run", None)]

    asyncio.run(scenario())


def test_schedule_toolkit_activates_groups_and_stops_on_failure():
    from schedule_resources import ScheduleRun

    async def scenario():
        called = []

        async def factory(**kwargs):
            called.append(True)
            return SimpleNamespace(
                tool_groups=[SimpleNamespace(name="basic"), SimpleNamespace(name="job")]
            )

        state = SimpleNamespace(tool_context=SimpleNamespace(activated_groups=[]))
        wrapped = schedule_toolkit(factory)
        token = schedule_run.set(ScheduleRun("u", "a", "s"))
        try:
            await wrapped(session_record=SimpleNamespace(state=state))
            assert state.tool_context.activated_groups == ["basic", "job"]
            schedule_run.get().error = "资源准备失败"
            try:
                await wrapped(session_record=SimpleNamespace(state=state))
                raise AssertionError("不应继续装配")
            except RuntimeError as exc:
                assert str(exc) == "资源准备失败"
            assert called == [True]
        finally:
            schedule_run.reset(token)

    asyncio.run(scenario())


def test_same_session_preparation_does_not_mutate_during_inference(monkeypatch):
    from contextlib import asynccontextmanager

    async def scenario():
        calls = []
        lock = asyncio.Lock()
        running, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Storage:
            async def get_session(self, *args):
                return SimpleNamespace(origin=ScheduleOrigin(schedule_id="schedule-1"))

        class Bus:
            attempts = 0

            @asynccontextmanager
            async def acquire_lock(self, *args, **kwargs):
                self.attempts += 1
                if self.attempts == 2:
                    attempted.set()
                async with lock:
                    yield

        class Chat:
            _storage = Storage()
            _message_bus = Bus()

            async def run(self, *args):
                calls.append("inference")
                running.set()
                await release.wait()

        async def request(_self, method, url, **kwargs):
            calls.append("prepare")
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

        monkeypatch.setattr(httpx.AsyncClient, "request", request)
        install_schedule_preparation(Chat, "http://nexus")
        user = "nexus-schedule-" + "a" * 64
        first = asyncio.create_task(Chat().run(user, "session", "agent"))
        await asyncio.wait_for(running.wait(), 1)
        second = asyncio.create_task(Chat().run(user, "session", "agent"))
        await asyncio.wait_for(attempted.wait(), 1)
        assert calls == ["prepare", "inference"]
        release.set()
        await asyncio.gather(first, second)
        assert calls == ["prepare", "inference", "prepare", "inference"]
        assert not lock.locked()

    asyncio.run(scenario())


def test_schedule_physical_directories_are_isolated_from_chat_and_other_arrangers(tmp_path):
    from pathlib import Path

    from agentscope.app.workspace_manager import IsolationPolicy
    from schedule_workspace import ScheduleWorkspaceManager

    async def scenario():
        manager = ScheduleWorkspaceManager(str(tmp_path), isolation=IsolationPolicy.PER_SESSION)
        try:
            chat = await manager.get_workspace("nexus", "agent", "chat", "same-id")
            alice = await manager.get_workspace("nexus-schedule-" + "a" * 64, "agent", "a", "same-id")
            bob = await manager.get_workspace("nexus-schedule-" + "b" * 64, "agent", "b", "same-id")
            assert chat.workdir == str(tmp_path / "agent")
            assert len({chat.workdir, alice.workdir, bob.workdir}) == 3
            Path(chat.workdir, "chat.txt").write_text("private chat")
            Path(alice.workdir, "alice.txt").write_text("alice schedule")
            assert not Path(alice.workdir, "chat.txt").exists()
            assert not Path(bob.workdir, "alice.txt").exists()
        finally:
            await manager.close_all()

    asyncio.run(scenario())
