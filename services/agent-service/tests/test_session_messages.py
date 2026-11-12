import asyncio
import os

import httpx
import pytest
from agentscope.app import create_app
from agentscope.app._service import _chat
from agentscope.app.message_bus import InMemoryMessageBus
from agentscope.app.storage import (
    AgentData,
    AgentRecord,
    AsyncSQLAlchemyStorage,
    SessionConfig,
)
from agentscope.app.workspace_manager import IsolationPolicy
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatModelBase, ChatResponse
from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import FunctionTool
from chat_attachments import read_attachment
from group_security import AgentServiceKeyMiddleware, install_run_context
from schedule_workspace import ScheduleWorkspaceManager
from session_messages import register_session_messages
from workspace_attachments import register_workspace_attachments


def test_native_history_and_private_retry_survive_partial_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "test-only")
    contexts = []
    reads = []
    errors = []
    key = "att_" + "f" * 32
    database_url = os.environ.get("AGENTSCOPE_TEST_MYSQL_URL", f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}")

    class Model(ChatModelBase):
        def __init__(self):
            super().__init__(OpenAICredential(api_key="test-only"), "test-model", self.Parameters())
            self.formatter = OpenAIChatFormatter()

        async def _call_api(self, model_name, messages, tools=None, **kwargs):
            contexts.append([block.text for message in messages for block in message.content if block.type == "text"])
            if len(contexts) == 1:
                return ChatResponse(content=[ToolCallBlock(id="call_read", name="read_file", input="{}")], is_last=True)
            return ChatResponse(content=[TextBlock(text="已收到")], is_last=True)

    async def model_for(*args):
        return Model()

    original_report = _chat.ChatService._report_failure

    async def report(self, user_id, session_id, agent_id, error, *args):
        errors.append(error)
        return await original_report(self, user_id, session_id, agent_id, error, *args)

    monkeypatch.setattr(_chat.ChatService, "_report_failure", report)
    monkeypatch.setattr(_chat, "get_model", model_for)
    monkeypatch.setattr(_chat.ChatService, "run", _chat.ChatService.run)
    install_run_context(_chat.ChatService)

    def message(id_, **metadata):
        return {"id": id_, "name": "用户", "role": "user", "content": [{"type": "text", "text": id_}],
                "metadata": metadata}

    async def scenario():
        async def tools(user_id, agent_id, session_id):
            async def read_file() -> str:
                """读取已发送的附件。"""
                data = await read_attachment(
                    storage, app.state.workspace_service, "http://nexus",
                    user_id, agent_id, session_id, "current-turn", key, 0, 1,
                )
                result = data.content[0].text
                reads.append(result)
                return result

            return [FunctionTool(read_file, is_read_only=True, permission=PermissionDecision(
                behavior=PermissionBehavior.ALLOW, message="测试读取",
            ))]

        def make_app():
            storage = AsyncSQLAlchemyStorage(database_url)
            bus = InMemoryMessageBus()
            manager = ScheduleWorkspaceManager(str(tmp_path / "ws"), isolation=IsolationPolicy.PER_SESSION)
            app = create_app(storage=storage, message_bus=bus, workspace_manager=manager, extra_agent_tools=tools)
            register_session_messages(app, storage, bus)
            register_workspace_attachments(app, storage, bus)
            app.add_middleware(AgentServiceKeyMiddleware)
            return app, storage

        app, storage = make_app()
        async with app.router.lifespan_context(app):
            agent = AgentRecord(user_id="nexus", data=AgentData(name="测试员工", system_prompt="处理群讨论"))
            await storage.upsert_agent("nexus", agent)
            session = await storage.upsert_session("nexus", agent.id, SessionConfig(
                workspace_id="test-chat",
                chat_model_config={
                    "type": "openai_credential", "credential_id": "test", "model": "test", "parameters": {},
                },
            ))
            binding = {"agent_id": agent.id, "session_id": session.id}
            original = storage.upsert_message

            async def fail_after_state(user_id, session_id, msg):
                if msg.id == "history-2":
                    raise OSError("模拟原生消息日志提交中断")
                await original(user_id, session_id, msg)

            headers = {"X-User-ID": "nexus", "X-Nexus-Internal-Key": "test-only"}
            async with httpx.AsyncClient(
                base_url="http://agent", transport=httpx.ASGITransport(app=app), headers=headers,
            ) as client:
                history = "/nexus/session-messages"
                body = {**binding, "messages": [message("history-1"), message("history-2")]}
                monkeypatch.setattr(storage, "upsert_message", fail_after_state)
                with pytest.raises(OSError, match="日志提交中断"):
                    await client.post(history, json=body)
                monkeypatch.setattr(storage, "upsert_message", original)
                assert (await client.post(history, json=body)).status_code == 204
                assert (await client.post(history, json=body)).status_code == 204
                assert (await client.post(history, json=body, headers={"X-User-ID": "other"})).status_code == 404
                assert (await client.post(history, json={**body, "agent_id": "other"})).status_code == 404
                assert (await client.get(f"{history}/history-1", params=binding)).status_code == 204
                assert (await client.get(
                    f"{history}/history-1", params={**binding, "agent_id": "other"},
                )).status_code == 404
                assert (await client.get(f"{history}/missing", params=binding)).status_code == 404
                persisted = await storage.get_session("nexus", agent.id, session.id)
                assert [msg.id for msg in persisted.state.context] == ["history-1", "history-2"]
                log, _ = await storage.list_messages("nexus", session.id)
                assert [msg.id for msg in log] == ["history-1", "history-2"]
                assert contexts == []

                path = f"/nexus/workspace-attachments/{key}"
                uploaded = await client.put(path, params=binding, content=b"workspace-file")
                assert uploaded.status_code == 200, uploaded.text
                user = message(
                    "current-turn", nexus_deduplicate=True,
                    attachments=[{"id": key, "name": "log.txt", "type": "text/plain"}],
                )
                post = {**binding, "input": user}
                assert (await client.post("/chat/", json=post)).status_code == 200
                receipt = await client.get(f"{history}/current-turn", params={**binding, "wait": "true"})
                assert receipt.status_code == 204, receipt.text
                async with asyncio.timeout(5):
                    while (await client.get(f"/sessions/{session.id}/status", params=binding)).json().get(
                        "status",
                    ) != "idle":
                        await asyncio.sleep(0.02)
                assert len(contexts) == 2, errors
                context = contexts[0]
                assert context.index("history-1") < context.index("history-2") < context.index("current-turn")
                assert any("read_chat_attachment" in block for block in context)
                assert reads == ["log.txt\nworkspace-file"]

        # 重启 Agent Service，重新连接同一原生 SQL 库后仍能识别已接受的 Msg.id。
        app, storage = make_app()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(base_url="http://agent", transport=httpx.ASGITransport(app=app), headers=headers) as client,
        ):
            assert (await client.get(f"{history}/current-turn", params=binding)).status_code == 204
            assert (await client.post("/chat/", json=post)).status_code == 200
            await app.state.chat_service.run("nexus", session.id, agent.id, Msg.model_validate(post["input"]))
        assert len(contexts) == 2
        assert reads == ["log.txt\nworkspace-file"]

    asyncio.run(scenario())
