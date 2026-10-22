"""真实官方 SQL + HTTP router + 调度触发验证，不调用模型供应商。"""

import asyncio
import hashlib

import httpx
from agentscope_sql import create_storage, initialize_database
from schedule_access import ScheduleResourceAccessPolicy


def user(uid):
    return "nexus-schedule-" + hashlib.sha256(uid.encode()).hexdigest()


def test_official_schedule_ownership_trigger_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "AGENTSCOPE_SQL_URL", f"sqlite+aiosqlite:///{tmp_path / 'schedules.db'}"
    )

    async def scenario():
        from agentscope.app import create_app
        from agentscope.app._manager import SchedulerManager
        from agentscope.app._service import ResourceAccessService, SessionService
        from agentscope.app.access import ResourceKind, ResourcePermission
        from agentscope.app.message_bus import InMemoryMessageBus, MessageBusKeys
        from agentscope.app.storage import AgentData, AgentRecord
        from agentscope.app.workspace_manager import (
            IsolationPolicy,
            LocalWorkspaceManager,
        )
        from agentscope.credential import OpenAICredential

        await initialize_database()
        bus = InMemoryMessageBus()
        workspace = LocalWorkspaceManager(
            str(tmp_path / "workspaces"), isolation=IsolationPolicy.PER_SESSION
        )
        policy = ScheduleResourceAccessPolicy("nexus")
        async with create_storage() as storage:
            agent = AgentRecord(user_id="nexus", data=AgentData(name="日程验证"))
            credential = OpenAICredential(
                name="gateway-test",
                api_key="test-only",
                base_url="http://127.0.0.1:1/v1",
            )
            await storage.upsert_agent("nexus", agent)
            await storage.upsert_credential("nexus", credential)
            assert (
                await policy.list_accessible(
                    "ordinary-user", ResourceKind.AGENT, storage
                )
                == []
            )
            refs = await policy.list_accessible(
                user("alice"), ResourceKind.AGENT, storage
            )
            assert len(refs) == 1 and refs[0].permission == ResourcePermission.READ
            assert not await policy.can_edit(
                user("alice"), ResourceKind.AGENT, "nexus", agent.id, storage
            )
            app = create_app(
                storage=storage,
                message_bus=bus,
                workspace_manager=workspace,
                resource_access_policy=policy,
                enable_scheduler=False,
            )
            app.state.scheduler_manager = SchedulerManager(
                storage, bus, workspace, enabled=True
            )
            app.state.resource_access_service = ResourceAccessService(storage, policy)
            app.state.session_service = SessionService(storage, bus, workspace)
            a, b = {"X-User-ID": user("alice")}, {"X-User-ID": user("bob")}
            payload = {
                "name": "晨报",
                "description": "日程触发内容",
                "cron_expression": "* * * * *",
                "timezone": "Asia/Shanghai",
                "agent_id": agent.id,
                "stateful": True,
                "chat_model_config": {
                    "type": "openai_credential",
                    "credential_id": credential.id,
                    "model": "test",
                    "parameters": {},
                },
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://service"
            ) as client:
                created = await client.post("/schedule/", json=payload, headers=a)
                assert created.status_code == 201, created.text
                sid = created.json()["schedule_id"]
                assert (await client.get("/schedule/", headers=b)).json()["total"] == 0
                for method, suffix, body in [
                    ("PATCH", "", {"enabled": False}),
                    ("DELETE", "", None),
                    ("GET", "/sessions", None),
                ]:
                    response = await client.request(
                        method, "/schedule/" + sid + suffix, headers=b, json=body
                    )
                    assert response.status_code == 404
                invalid = await client.post(
                    "/schedule/",
                    headers=a,
                    json={**payload, "timezone": "bad/timezone"},
                )
                assert invalid.status_code == 422
                invalid = await client.post(
                    "/schedule/", headers=a, json={**payload, "cron_expression": "bad"}
                )
                assert invalid.status_code == 422
                record = await storage.get_schedule(user("alice"), sid)
                scheduler = app.state.scheduler_manager
                await scheduler.reconcile()
                assert len(scheduler._scheduler.get_jobs()) == 1
                # 使用官方触发实现，验证实际建 Session、workspace 和 inbox/wakeup 投递。
                await scheduler._build_trigger(record)()
                sessions = await storage.list_sessions_by_schedule(user("alice"), sid)
                assert len(sessions) == 1
                session = sessions[0]
                assert session.config.workspace_id
                assert (
                    await storage.get_session(user("bob"), agent.id, session.id) is None
                )
                inbox = await bus.queue_drain(MessageBusKeys.inbox(session.id))
                assert len(inbox) == 1 and "日程触发内容" in str(inbox)
                assert await bus.queue_drain(MessageBusKeys.wakeup_queue())
                await scheduler._build_trigger(record)()
                assert (
                    len(await storage.list_sessions_by_schedule(user("alice"), sid))
                    == 1
                )
                paused = await client.patch(
                    "/schedule/" + sid, headers=a, json={"enabled": False}
                )
                assert paused.status_code == 200
                await scheduler.reconcile()
                assert scheduler._scheduler.get_jobs() == []
                await bus.queue_drain(MessageBusKeys.inbox(session.id))
                await scheduler._build_trigger(
                    await storage.get_schedule(user("alice"), sid)
                )()
                assert await bus.queue_drain(MessageBusKeys.inbox(session.id)) == []
            saved_sid = sid
        async with create_storage() as restarted:
            assert (
                await restarted.get_schedule(user("alice"), saved_sid)
            ).data.enabled is False
            assert await restarted.get_schedule(user("bob"), saved_sid) is None

    asyncio.run(scenario())
