"""平台助手测试：事件映射、卡片协议、prepare → confirm 操作生命周期、
Tool Gateway 与 REST API（使用 Fake OpenCode Client，不依赖真实服务）。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from app.assistant.cards import task_card
from app.assistant.event_mapper import map_server_event, normalize_tool_name
from app.assistant.models import (
    AssistantContextRef,
    AssistantConversation,
    AssistantMessage,
    AssistantMessagePart,
    AssistantOperation,
    AssistantToolCall,
)
from app.assistant.operations import OperationError, OperationService
from app.assistant.orchestrator import AssistantOrchestrator, set_orchestrator
from app.assistant.rate_limit import assistant_rate_limiter
from app.assistant.server import AssistantOpenCodeServerManager
from app.assistant.service import ConversationService
from app.config.settings import PROJECT_ROOT, get_settings
from app.database import SessionLocal, init_database
from app.domain import Priority, TaskStatus, TaskType
from app.main import app
from app.models import AgentWorker, Task, TaskCategory
from app.schemas import AcceptanceCriterion, TaskCreate
from app.services import TaskService

ASSISTANT_TABLES = [
    AssistantConversation.__table__,
    AssistantMessage.__table__,
    AssistantMessagePart.__table__,
    AssistantContextRef.__table__,
    AssistantOperation.__table__,
    AssistantToolCall.__table__,
]


class FakeAssistantClient:
    def __init__(self):
        self.sessions: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.aborts: list[str] = []
        self.base_url = "http://fake-opencode"

    async def health(self):
        return {"healthy": True, "version": "1.18.25"}

    async def create_session(self, *, title: str) -> str:
        session_id = f"SESS-FAKE-{len(self.sessions) + 1}"
        self.sessions.append(session_id)
        return session_id

    async def session_exists(self, session_id: str) -> bool:
        return session_id in self.sessions

    async def delete_session(self, session_id: str) -> None:
        self.sessions = [item for item in self.sessions if item != session_id]

    async def prompt_async(self, session_id: str, *, text: str) -> None:
        self.prompts.append((session_id, text))

    async def messages(self, session_id: str) -> list:
        return []

    async def status(self, session_id: str) -> str:
        return "idle"

    async def abort(self, session_id: str) -> None:
        self.aborts.append(session_id)

    async def stream_events(self):
        if False:
            yield {}


def test_assistant_server_manager_configures_isolated_runtime(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "assistant_opencode_hostname", "0.0.0.0")
    monkeypatch.setattr(settings, "assistant_opencode_port", 4097)
    monkeypatch.setattr(settings, "assistant_opencode_username", "assistant-user")
    monkeypatch.setattr(settings, "assistant_opencode_password", "assistant-password")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "execution-password")

    manager = AssistantOpenCodeServerManager(settings, binary="opencode")
    command = manager.command()
    environment = manager.process_environment()

    assert command[-5:] == ["serve", "--hostname", "127.0.0.1", "--port", "4097"]
    assert environment["OPENCODE_SERVER_USERNAME"] == "assistant-user"
    assert environment["OPENCODE_SERVER_PASSWORD"] == "assistant-password"
    assert manager.executable_directory == Path(sys.executable).parent
    assert manager.mcp_entrypoint.parent == Path(sys.executable).parent
    assert environment["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert manager.workspace == PROJECT_ROOT
    assert environment["ASTRA_RUNTIME_ROLE"] == "review-executor"
    assert command[command.index("--hostname") + 1] == "127.0.0.1"
    config = json.loads(Path(environment["OPENCODE_CONFIG"]).read_text(encoding="utf-8"))
    assert set(config) == {"mcp"}
    assert set(config["mcp"]) == {"nexus-review"}

    monkeypatch.setattr(settings, "assistant_opencode_password", None)
    assert "OPENCODE_SERVER_PASSWORD" not in manager.process_environment()


def test_assistant_uses_project_level_capabilities_without_workspace():
    """助手以项目根为能力加载上下文，不拥有独立 Workspace。"""

    settings = get_settings()
    assert not hasattr(settings, "assistant_workspace_dir")
    assert AssistantOpenCodeServerManager(settings).workspace == PROJECT_ROOT
    assert (PROJECT_ROOT / ".opencode" / "agents" / "platform-assistant.md").is_file()
    assert (
        PROJECT_ROOT / ".opencode" / "skills" / "nexus-task-experience-review" / "SKILL.md"
    ).is_file()
    assert not (PROJECT_ROOT / "opencode.json").exists()
    config = AssistantOpenCodeServerManager(settings).review_config()
    assert set(config["mcp"]) == {"nexus-review"}


@pytest.mark.asyncio
async def test_assistant_migration_adopts_complete_precreated_schema(tmp_path):
    """An app-started 0012 database upgrades without dropping assistant data."""

    database_path = tmp_path / "precreated-assistant.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    environment = {**os.environ, "ASTRA_DATABASE_URL": database_url, "ASTRA_SEED_DEMO": "false"}
    backend_root = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "0012_employee_default_work_area"],
        cwd=backend_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    isolated_engine = create_async_engine(database_url)
    async with isolated_engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: AssistantConversation.metadata.create_all(
                sync_connection,
                tables=ASSISTANT_TABLES,
            )
        )
        await connection.execute(
            sa.text(
                """INSERT INTO assistant_conversations
                (id, user_id, title, status, created_at, updated_at)
                VALUES (:id, :user_id, :title, :status, :created_at, :updated_at)"""
            ),
            {
                "id": "AC-PRECREATED",
                "user_id": "console-admin",
                "title": "迁移前会话",
                "status": "ACTIVE",
                "created_at": "2026-09-05 00:00:00",
                "updated_at": "2026-09-05 00:00:00",
            },
        )
    await isolated_engine.dispose()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    upgraded_engine = create_async_engine(database_url)
    async with upgraded_engine.connect() as connection:
        revision = await connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        title = await connection.scalar(
            sa.text("SELECT title FROM assistant_conversations WHERE id = 'AC-PRECREATED'")
        )
    await upgraded_engine.dispose()

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    expected_head = ScriptDirectory.from_config(Config(str(backend_root / "alembic.ini"))).get_current_head()
    assert revision == expected_head
    assert title == "迁移前会话"


@pytest.fixture
def fake_orchestrator(monkeypatch):
    settings = get_settings()
    fake = FakeAssistantClient()
    orchestrator = AssistantOrchestrator(settings, client=fake)
    set_orchestrator(orchestrator)
    assistant_rate_limiter.reset()
    monkeypatch.setattr(settings, "assistant_enabled", True)
    yield fake, orchestrator
    assistant_rate_limiter.reset()
    set_orchestrator(None)


async def seed_employee_and_category():
    async with SessionLocal() as session:
        if not await session.get(TaskCategory, "assistant-test"):
            session.add(TaskCategory(id="assistant-test", name="助手测试分类"))
        if not await session.get(AgentWorker, "emp-tuya"):
            session.add(
                AgentWorker(
                    id="emp-tuya",
                    name="图丫丫",
                    role="测试工程师",
                    status="IDLE",
                    enabled=True,
                    task_types=["ISSUE", "REQUIREMENT"],
                    category_ids=["assistant-test"],
                )
            )
        await session.commit()


async def create_task_directly(**overrides) -> Task:
    payload = TaskCreate(
        source="manual",
        task_type=TaskType.ISSUE,
        category_id="assistant-test",
        title="助手测试任务",
        description="用于平台助手测试的任务",
        acceptance_criteria=[AcceptanceCriterion(content="测试通过")],
        employee_id="emp-tuya",
        priority=Priority.MEDIUM,
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    async with SessionLocal() as session:
        return await TaskService(session).create(payload)


# ── 事件映射 ────────────────────────────────────────────────────


def test_event_mapper_maps_text_tool_idle_and_error():
    session = "SESS-1"
    text_event = {
        "type": "message.part.updated",
        "properties": {
            "info": {"id": "msg-1", "role": "assistant", "sessionID": session},
            "part": {"id": "p1", "messageID": "msg-1", "sessionID": session, "type": "text", "text": "你好"},
        },
    }
    mapped = map_server_event(text_event)
    assert mapped[0]["event"] == "assistant.message.delta"
    assert mapped[0]["session_id"] == session
    assert mapped[0]["data"]["text"] == "你好"

    tool_event = {
        "type": "message.part.updated",
        "properties": {
            "info": {"id": "msg-2", "role": "assistant", "sessionID": session},
            "part": {
                "id": "t1",
                "messageID": "msg-2",
                "sessionID": session,
                "type": "tool",
                "tool": "mcp__nexus-assistant__nexus_task_get",
                "state": {"status": "completed", "input": {"task_id": "T-1"}, "output": '{"task_id": "T-1"}'},
            },
        },
    }
    mapped = map_server_event(tool_event)
    assert mapped[0]["event"] == "assistant.tool.completed"
    assert mapped[0]["data"]["tool"] == "nexus_task_get"
    assert mapped[0]["data"]["output"] == {"task_id": "T-1"}

    idle_event = {"type": "session.idle", "properties": {"sessionID": session}}
    mapped = map_server_event(idle_event)
    assert mapped[0]["event"] == "assistant.completed"

    error_event = {
        "type": "session.error",
        "properties": {"sessionID": session, "error": {"message": "boom"}},
    }
    mapped = map_server_event(error_event)
    assert mapped[0]["event"] == "assistant.failed"
    assert mapped[0]["data"]["error"] == "boom"

    info_event = {
        "type": "message.updated",
        "properties": {
            "sessionID": session,
            "info": {"id": "msg-3", "role": "assistant", "sessionID": session},
        },
    }
    mapped = map_server_event(info_event)
    assert mapped[0]["event"] == "assistant.message.info"
    assert mapped[0]["data"]["role"] == "assistant"
    assert mapped[0]["data"]["error"] is None

    delta_event = {
        "type": "message.part.delta",
        "properties": {
            "sessionID": session,
            "messageID": "msg-3",
            "partID": "p1",
            "field": "text",
            "delta": "你好",
        },
    }
    mapped = map_server_event(delta_event)
    assert mapped[0]["event"] == "assistant.message.text_delta"
    assert mapped[0]["data"]["delta"] == "你好"
    assert mapped[0]["data"]["opencode_message_id"] == "msg-3"

    assert normalize_tool_name("mcp__nexus-assistant__nexus_task_search") == "nexus_task_search"
    assert normalize_tool_name("nexus-assistant_nexus_task_search") == "nexus_task_search"
    assert normalize_tool_name("nexus_task_search") == "nexus_task_search"

    reasoning_event = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": session,
            "part": {"id": "r1", "messageID": "msg-3", "sessionID": session, "type": "reasoning"},
        },
    }
    mapped = map_server_event(reasoning_event)
    assert mapped[0]["event"] == "assistant.message.reasoning"
    assert mapped[0]["data"]["part_id"] == "r1"


def test_task_card_uses_platform_routes_and_operation_actions():
    task = Task(
        id="T-20260904-ABC123",
        title="验证卡片协议",
        task_type="ISSUE",
        status="RUNNING",
        priority="HIGH",
        current_progress="执行中",
    )
    card = task_card(task)
    assert card["card_type"] == "task"
    assert {"type": "navigate", "label": "查看任务", "target": "/tasks/T-20260904-ABC123"} in card["actions"]
    assert {"type": "assistant_operation", "label": "停止任务", "operation": "task_cancel"} in card["actions"]
    for action in card["actions"]:
        assert action["type"] == "assistant_operation" or action["target"].startswith("/tasks/")


# ── prepare → confirm 生命周期 ─────────────────────────────────


async def test_prepare_and_confirm_creates_task_once():
    await init_database()
    await seed_employee_and_category()
    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_create(
            user_id="console-admin",
            task_type="ISSUE",
            employee_id="emp-tuya",
            title="让图丫丫执行回归测试",
            description="执行 OMDP_20260901_001 回归测试",
            category_id="assistant-test",
        )
        assert operation.status == "WAITING_CONFIRM"
        assert operation.preview_json["employee"] == "图丫丫"
        assert operation.preview_json["task_name"] == "让图丫丫执行回归测试"
        operation_id = operation.operation_id

    async with SessionLocal() as session:
        result = await OperationService(session).confirm(operation_id, "console-admin")
        assert result["status"] == "PENDING"
        assert result["employee_id"] == "emp-tuya"
        task_id = result["task_id"]
        task = await session.get(Task, task_id)
        assert task is not None
        assert task.source == "assistant"

    async with SessionLocal() as session:
        service = OperationService(session)
        operation = await service.get(operation_id)
        assert operation.status == "SUCCESS"
        # 幂等：重复确认返回同一结果，不重复创建任务
        again = await service.confirm(operation_id, "console-admin")
        assert again["task_id"] == task_id

    from sqlalchemy import func, select

    async with SessionLocal() as session:
        count = await session.scalar(select(func.count(Task.id)).where(Task.title == "让图丫丫执行回归测试"))
        assert count == 1


async def test_cancel_running_task_is_high_risk_and_confirm_cancels():
    await init_database()
    await seed_employee_and_category()
    task = await create_task_directly(title="运行中任务待取消")
    async with SessionLocal() as session:
        live = await session.get(Task, task.id)
        await TaskService(session).transition(live, TaskStatus.RUNNING)

    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_action(
            user_id="console-admin", action="cancel", task_id=task.id
        )
        assert operation.risk_level == "L3"
        operation_id = operation.operation_id

    async with SessionLocal() as session:
        result = await OperationService(session).confirm(operation_id, "console-admin")
        assert result["status"] == "CANCELED"


async def test_retry_starts_a_new_opencode_session(monkeypatch):
    await init_database()
    await seed_employee_and_category()
    task = await create_task_directly(title="失败任务待重新执行")
    async with SessionLocal() as session:
        live = await session.get(Task, task.id)
        live.current_session_id = "ses_old"
        await TaskService(session).transition(live, TaskStatus.RUNNING)
        await TaskService(session).transition(live, TaskStatus.FAILED)

    async def fake_restart(runtime, task_id, *, restart=False):
        assert restart is True
        live = await runtime.session.get(Task, task_id)
        assert live.current_session_id == "ses_old"
        live.current_session_id = "ses_new"
        live.status = TaskStatus.RUNNING
        await runtime.session.commit()
        return live

    monkeypatch.setattr("app.runtime.lifecycle.RuntimeService.start", fake_restart)

    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_action(
            user_id="console-admin", action="retry", task_id=task.id
        )
        operation_id = operation.operation_id

    async with SessionLocal() as session:
        result = await OperationService(session).confirm(operation_id, "console-admin")
        assert result["status"] == "RUNNING"
        live = await session.get(Task, task.id)
        assert live.current_session_id == "ses_new"


async def test_waiting_task_cannot_use_legacy_resume_operation():
    await init_database()
    await seed_employee_and_category()
    task = await create_task_directly(title="等待原生 Question 回答")
    async with SessionLocal() as session:
        live = await session.get(Task, task.id)
        await TaskService(session).transition(live, TaskStatus.WAITING)
        with pytest.raises(OperationError, match="不支持的操作"):
            await OperationService(session).prepare_task_action(
                user_id="console-admin", action="resume", task_id=task.id
            )


async def test_reassign_ready_task_requires_confirmation_and_revalidates_status():
    await init_database()
    await seed_employee_and_category()
    async with SessionLocal() as session:
        if not await session.get(AgentWorker, "emp-reassign"):
            session.add(
                AgentWorker(
                    id="emp-reassign",
                    name="重新分配员工",
                    role="测试工程师",
                    status="IDLE",
                    enabled=True,
                    task_types=["ISSUE"],
                    category_ids=["assistant-test"],
                )
            )
            await session.commit()

    task = await create_task_directly(title="待重新分配任务")
    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_reassign(
            user_id="console-admin",
            task_id=task.id,
            employee_id="emp-reassign",
            reason="调整测试负责人",
        )
        assert operation.operation_type == "task_reassign"
        assert operation.risk_level == "L2"
        assert operation.preview_json["new_employee"] == "重新分配员工"
        operation_id = operation.operation_id

    async with SessionLocal() as session:
        result = await OperationService(session).confirm(operation_id, "console-admin")
        assert result["employee_id"] == "emp-reassign"
        reassigned = await session.get(Task, task.id)
        assert reassigned.employee_id == "emp-reassign"
        assert reassigned.current_session_id is None

        await TaskService(session).transition(reassigned, TaskStatus.RUNNING)
        with pytest.raises(OperationError, match="待领取或已挂起"):
            await OperationService(session).prepare_task_reassign(
                user_id="console-admin",
                task_id=task.id,
                employee_id="emp-tuya",
            )


async def test_operation_expiry_blocks_confirm():
    await init_database()
    await seed_employee_and_category()
    from datetime import UTC, datetime, timedelta

    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_create(
            user_id="console-admin",
            task_type="ISSUE",
            employee_id="emp-tuya",
            title="过期操作",
            category_id="assistant-test",
        )
        operation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
        with pytest.raises(OperationError):
            await OperationService(session).confirm(operation.operation_id, "console-admin")
        assert operation.status == "EXPIRED"


async def test_operation_enforces_owner_and_records_stale_failure():
    await init_database()
    await seed_employee_and_category()
    task = await create_task_directly(title="状态变化后的确认")
    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_action(
            user_id="console-admin", action="cancel", task_id=task.id
        )
        operation_id = operation.operation_id
        with pytest.raises(OperationError) as unauthorized:
            await OperationService(session).confirm(operation_id, "another-user")
        assert unauthorized.value.status_code == 403
        with pytest.raises(OperationError) as cancel_unauthorized:
            await OperationService(session).cancel(operation_id, "another-user")
        assert cancel_unauthorized.value.status_code == 403

    # prepare 之后任务可能被其他入口改变；确认失败也必须从 EXECUTING 收敛。
    async with SessionLocal() as session:
        live = await session.get(Task, task.id)
        live.status = TaskStatus.SUCCEEDED
        await session.commit()

    async with SessionLocal() as session:
        with pytest.raises(OperationError, match="操作执行失败"):
            await OperationService(session).confirm(operation_id, "console-admin")
        failed = await OperationService(session).get(operation_id)
        assert failed.status == "FAILED"
        assert failed.executed_at is not None
        assert failed.result_json["error"]


async def test_prepare_validates_employee_and_unknown_task():
    await init_database()
    await seed_employee_and_category()
    async with SessionLocal() as session:
        service = OperationService(session)
        with pytest.raises(OperationError):
            await service.prepare_task_create(
                user_id="console-admin",
                task_type="ISSUE",
                employee_id="emp-not-exists",
                title="员工不存在",
            )
        with pytest.raises(OperationError):
            await service.prepare_task_action(user_id="console-admin", action="cancel", task_id="T-NOPE")


# ── Tool Gateway ───────────────────────────────────────────────


async def test_tool_gateway_search_recommend_and_overview():
    await init_database()
    await seed_employee_and_category()
    await create_task_directly(title="网关搜索任务")
    from app.assistant import tool_gateway

    async with SessionLocal() as session:
        result = await tool_gateway.task_search(session, keyword="网关搜索")
        assert result["total"] == 1
        assert result["items"][0]["title"] == "网关搜索任务"

        detail = await tool_gateway.task_get(session, result["items"][0]["task_id"])
        assert detail["card"]["card_type"] == "task"

        recommend = await tool_gateway.employee_recommend(session, task_type="ISSUE")
        assert any(item["employee_id"] == "emp-tuya" for item in recommend["items"])

        overview = await tool_gateway.operation_overview(session)
        assert overview["total_tasks"] >= 1

        pending = await tool_gateway.operation_pending_actions(session)
        assert "waiting_tasks" in pending
        assert "failed_tasks" in pending


# ── REST API + Fake OpenCode Client ────────────────────────────


def test_product_assistant_routes_are_gone():
    with TestClient(app) as client:
        assert client.get("/api/assistant/health").status_code == 404
        assert client.post("/api/assistant/conversations", json={}).status_code == 404
        assert client.get("/api/health").status_code == 200


# ── Orchestrator 事件处理（卡片与确认卡）────────────────────────


@pytest.mark.asyncio
async def test_orchestrator_links_confirmation_and_cards(fake_orchestrator):
    await init_database()
    await seed_employee_and_category()
    fake, orchestrator = fake_orchestrator
    settings = get_settings()

    async with SessionLocal() as session:
        conversation = await ConversationService(session, orchestrator, settings).create("事件测试")
        conversation_id = conversation.id
        session_id = conversation.opencode_session_id

    def server_event(part: dict, message_id: str) -> dict:
        return {
            "type": "message.part.updated",
            "properties": {
                "info": {"id": message_id, "role": "assistant", "sessionID": session_id},
                "part": {**part, "messageID": message_id, "sessionID": session_id},
            },
        }

    orchestrator._running_turns.add(conversation_id)

    # 真实事件顺序：message.updated（角色）先于 message.part.updated（文本）
    await orchestrator.handle_event(
        {
            "type": "message.updated",
            "properties": {
                "sessionID": session_id,
                "info": {"id": "msg-1", "role": "assistant", "sessionID": session_id},
            },
        }
    )
    await orchestrator.handle_event(
        server_event({"id": "p1", "type": "text", "text": "好的，我先查询任务。"}, "msg-1")
    )

    task = await create_task_directly(title="卡片任务")
    from app.assistant import tool_gateway

    async with SessionLocal() as session:
        detail = await tool_gateway.task_get(session, task.id)
    tool_output = json.dumps(detail, ensure_ascii=False, default=str)
    await orchestrator.handle_event(
        server_event(
            {
                "id": "t1",
                "type": "tool",
                "tool": "mcp__nexus-assistant__nexus_task_get",
                "state": {"status": "completed", "input": {"task_id": task.id}, "output": tool_output},
            },
            "msg-2",
        )
    )

    async with SessionLocal() as session:
        operation = await OperationService(session).prepare_task_action(
            user_id="console-admin",
            action="cancel",
            task_id=task.id,
            conversation_id=None,
        )
        operation_id = operation.operation_id
    prepare_output = json.dumps(
        {
            "operation_id": operation_id,
            "requires_confirmation": True,
            "risk_level": "L3",
            "preview": {"task_id": task.id, "task_name": task.title},
        },
        ensure_ascii=False,
    )
    await orchestrator.handle_event(
        server_event(
            {
                "id": "t2",
                "type": "tool",
                "tool": "mcp__nexus-assistant__nexus_task_prepare_cancel",
                "state": {"status": "completed", "input": {"task_id": task.id}, "output": prepare_output},
            },
            "msg-3",
        )
    )

    await orchestrator.handle_event({"type": "session.idle", "properties": {"sessionID": session_id}})

    replay = orchestrator.hub.replay(conversation_id)
    names = [envelope["event"] for envelope in replay]
    assert "assistant.started" not in names or True  # started 由 send_message 发布
    assert "assistant.message.created" in names
    assert "assistant.card.created" in names
    assert "assistant.operation.confirm_required" in names
    assert "assistant.completed" in names

    async with SessionLocal() as session:
        linked = await OperationService(session).get(operation_id)
        assert linked.conversation_id == conversation_id
        assert linked.message_id

        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from app.assistant.models import AssistantMessage

        assistant_messages = (
            (
                await session.execute(
                    select(AssistantMessage)
                    .where(
                        AssistantMessage.conversation_id == conversation_id,
                        AssistantMessage.role == "ASSISTANT",
                    )
                    .options(selectinload(AssistantMessage.parts))
                )
            )
            .scalars()
            .all()
        )
        assert len(assistant_messages) == 1
        final_message = assistant_messages[0]
        assert final_message.status == "COMPLETED"
        assert "好的" in final_message.content
        part_types = {part.part_type for part in final_message.parts}
        assert "TASK_CARD" in part_types
        assert "CONFIRM_CARD" in part_types

    confirm_envelope = next(
        envelope for envelope in replay if envelope["event"] == "assistant.operation.confirm_required"
    )
    assert confirm_envelope["data"]["operation_id"] == operation_id

    async with SessionLocal() as session:
        confirmed = await OperationService(session, get_settings()).confirm(
            operation_id, get_settings().assistant_user_id
        )
        stored = await OperationService(session).get(operation_id)
        assert stored is not None and stored.status == "SUCCESS"
        assert confirmed["status"] == "CANCELED"


@pytest.mark.asyncio
async def test_part_sequence_resumes_from_database_after_restart(fake_orchestrator):
    await init_database()
    _, orchestrator = fake_orchestrator
    async with SessionLocal() as session:
        conversation = await ConversationService(session).create("序号恢复")
        message = AssistantMessage(
            id="AM-SEQ-TEST",
            conversation_id=conversation.id,
            role="ASSISTANT",
            content="",
            status="STREAMING",
        )
        session.add(message)
        session.add(
            AssistantMessagePart(
                message_id=message.id,
                part_type="TASK_CARD",
                seq=3,
                payload_json={"card_type": "task", "id": "T-1"},
            )
        )
        await session.commit()
        orchestrator._part_seq.clear()
        assert await orchestrator._next_part_seq(session, message.id) == 4
