"""星系首页只读投影的聚合契约测试。

不启动 AgentScope，也不经真实业务写入口：直接准备员工目录、Worker、任务和
职责请求事实，验证投影只读取、不推断、不修改。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import get_session
from app.models import (
    AgentWorker,
    Base,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    WorkerInstance,
)
from nexus_api import nebula
from nexus_api.db import connect, init_db


def _insert_employee(connection, employee_id: str, name: str, agent_id: str | None, enabled: bool = True):
    connection.execute(
        """
        INSERT INTO t_nexus_employees (
            id, name, domain, description, instructions, avatar, enabled,
            knowledge_json, skills_json, mcp_json, tools_json,
            agentscope_agent_id, mailbox_session_id, capability_json,
            model_route_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', '[]', '[]', '[]', ?, ?, '{}', '', ?, ?)
        """,
        (
            employee_id,
            name,
            f"{name}领域",
            f"{name}职责",
            "直接回答",
            "✦",
            1 if enabled else 0,
            agent_id,
            f"mb_{employee_id}",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    connection.commit()


@pytest.fixture()
async def facts(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'platform.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    chat_path = tmp_path / "nexus.db"
    init_db(chat_path)
    chat = connect(chat_path)
    try:
        yield sessions, chat
    finally:
        chat.close()
        await engine.dispose()


async def _worker(session, worker_id, owner, *, mode="LOCAL", env_status="AVAILABLE", instance=None):
    worker = AgentWorker(
        id=worker_id,
        digital_employee_id=owner,
        name=f"Worker {worker_id}",
        role="自动化执行",
        status="IDLE",
        enabled=True,
        task_types=["ISSUE"],
        category_ids=[],
        last_heartbeat=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
    )
    session.add(worker)
    await session.flush()
    environment = OpenCodeExecutionEnvironment(
        employee_id=worker_id,
        mode=mode,
        name=f"环境 {worker_id}",
        worker_channel="worker" if mode == "LINUX_REMOTE" else "ssh",
        status=env_status,
        labels=["自动化"],
    )
    session.add(environment)
    await session.flush()
    if instance is not None:
        status, heartbeat = instance
        session.add(
            WorkerInstance(
                id=f"wi_{worker_id}",
                environment_id=environment.id,
                token_digest="x" * 64,
                status=status,
                last_heartbeat_at=heartbeat,
            )
        )
    return worker


def _task(
    task_id,
    *,
    status,
    owner=None,
    worker=None,
    title="任务",
    completed_at=None,
    runtime_connection="CONNECTED",
    waiting_reason=None,
    priority="MEDIUM",
):
    return Task(
        id=task_id,
        task_type="ISSUE",
        title=title,
        status=status,
        responsible_employee_id=owner,
        employee_id=worker,
        priority=priority,
        runtime_connection=runtime_connection,
        waiting_reason=waiting_reason,
        completed_at=completed_at,
    )


@pytest.mark.asyncio
async def test_snapshot_builds_galaxies_edges_and_marks_conflicts(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    _insert_employee(chat, "e2", "星二", "agent-2")
    _insert_employee(chat, "e3", "星三", None)

    now = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    async with sessions() as session:
        await _worker(session, "w1", "e1")
        await _worker(
            session,
            "w2",
            "e2",
            mode="LINUX_REMOTE",
            instance=("online", now - timedelta(seconds=10)),
        )
        await _worker(session, "w3", None, env_status="UNKNOWN")
        await _worker(session, "w4", "ghost", env_status="UNKNOWN")
        session.add_all(
            [
                _task("t1", status="PENDING", owner="e1"),
                _task(
                    "t2",
                    status="RUNNING",
                    owner="e1",
                    worker="w1",
                    runtime_connection="UNAVAILABLE",
                    waiting_reason="TRANSPORT_RECOVERY",
                ),
                _task("t3", status="WAITING", owner="e1", worker="w1"),
                _task("t4", status="RUNNING", owner="e2", worker="w2"),
                _task("t5", status="FAILED", owner="e3"),
                _task("t6", status="RUNNING", owner="e1", worker="w2"),
                _task("t7", status="FAILED", owner=None, worker="w3"),
                _task(
                    "t8",
                    status="SUCCEEDED",
                    owner="e1",
                    completed_at=now - timedelta(minutes=5),
                ),
                _task("t9", status="RUNNING", owner=None, worker="w1"),
                _task("t10", status="RUNNING", owner="e2"),
            ]
        )
        session.add(
            RuntimeSession(
                id="ses-2",
                task_id="t2",
                employee_id="w1",
                workspace="/tmp",
                environment={},
            )
        )
        await session.commit()

        snapshot = await nebula.collect_snapshot(session, chat, now=now)

    employees = {item["id"]: item for item in snapshot["employees"]}
    assert employees["e1"]["counts"] == {
        "running": 3,
        "queued": 1,
        "waiting": 1,
        "failed": 0,
        "completedToday": 1,
    }
    assert employees["e1"]["workerIds"] == ["w1"]
    assert employees["e3"]["interactionStatus"] == "unknown"
    assert employees["e2"]["interactionStatus"] == "available"

    workers = {item["id"]: item for item in snapshot["workers"]}
    assert workers["w1"]["health"] == "online"
    assert workers["w1"]["occupancy"] == "waiting"
    assert workers["w1"]["taskCount"] == 3
    assert workers["w2"]["health"] == "online"
    assert workers["w2"]["workerInstanceId"] == "wi_w2"
    assert workers["w4"]["health"] == "unknown"

    edges = {
        (item["digitalEmployeeId"], item["workerId"]): item
        for item in snapshot["taskEdges"]
    }
    assert edges[("e1", "w1")]["running"] == 2
    assert edges[("e1", "w1")]["waiting"] == 1
    assert edges[("e1", "w1")]["unavailable"] == 1
    assert edges[("e2", "w2")]["running"] == 1
    assert ("e1", "w2") not in edges  # 责任员工与 Worker 归属冲突时不画线

    conflict_codes = {item["code"] for item in snapshot["dataConflicts"]}
    assert "TASK_OWNER_CONFLICT" in conflict_codes
    assert "WORKER_EMPLOYEE_MISSING" in conflict_codes
    assert snapshot["unassigned"]["workerCount"] == 2
    assert snapshot["unassigned"]["taskCount"] == 1
    assert snapshot["unassigned"]["counts"]["failed"] == 1
    assert snapshot["totals"]["tasksQueued"] == 1
    assert snapshot["totals"]["tasksWaiting"] == 1
    assert snapshot["totals"]["tasksRunning"] == 5
    assert snapshot["totals"]["tasksFailed"] == 2
    assert snapshot["totals"]["completedToday"] == 1
    assert snapshot["degradedSources"] == []
    assert snapshot["snapshotVersion"]

    detail = await nebula.collect_employee_detail(session, chat, "e1", now=now)
    assert detail["employee"]["id"] == "e1"
    assert {item["id"] for item in detail["activeTasks"]} == {"t2", "t3", "t6", "t9"}

    single = await nebula.collect_task_detail(session, chat, "t2", now=now)
    assert single["task"]["id"] == "t2"
    assert single["task"]["ownerId"] == "e1"
    assert single["task"]["waitingReason"] == "执行连接恢复中，等待系统重连"
    with pytest.raises(ValueError):
        await nebula.collect_task_detail(session, chat, "missing", now=now)


@pytest.mark.asyncio
async def test_completed_today_uses_configured_timezone(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    now = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)  # 上海时间 1 月 2 日 08:30
    async with sessions() as session:
        await _worker(session, "w1", "e1")
        session.add_all(
            [
                _task(
                    "today",
                    status="SUCCEEDED",
                    owner="e1",
                    completed_at=datetime(2026, 1, 1, 16, 30, tzinfo=UTC),
                ),
                _task(
                    "yesterday",
                    status="SUCCEEDED",
                    owner="e1",
                    completed_at=datetime(2026, 1, 1, 15, 30, tzinfo=UTC),
                ),
                _task("no-timestamp", status="SUCCEEDED", owner="e1"),
                _task(
                    "unassigned-today",
                    status="SUCCEEDED",
                    completed_at=datetime(2026, 1, 1, 17, 0, tzinfo=UTC),
                ),
            ]
        )
        await session.commit()
        snapshot = await nebula.collect_snapshot(session, chat, now=now)
    assert snapshot["totals"]["completedToday"] == 2
    assert snapshot["employees"][0]["counts"]["completedToday"] == 1
    assert snapshot["unassigned"]["counts"]["completedToday"] == 1


@pytest.mark.asyncio
async def test_collaboration_dedupes_directions_and_hides_unowned_conversations(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    _insert_employee(chat, "e2", "星二", "agent-2")
    chat.execute(
        """
        INSERT INTO t_nexus_conversations (
            id, employee_id, owner_id, kind, name, created_at, updated_at
        ) VALUES (
            'conv-1', 'e1', 'u1', 'private', '对话',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        )
        """
    )
    now = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    rows = [
        ("d1", "private:conv-1", "agent-1", "agent-2", "请协助", "2026-01-02T10:00:00+00:00"),
        ("d2", "private:conv-1", "agent-1", "agent-2", "请协助", "2026-01-02T10:05:00+00:00"),
        ("d3", "private:conv-1", "agent-2", "agent-1", "收到", "2026-01-02T11:00:00+00:00"),
        ("d4", "private:conv-1", "agent-1", "agent-2", "另一件事", "2025-12-31T10:00:00+00:00"),
        ("d5", "private:conv-1", "agent-1", "ghost", "找不到人", "2026-01-02T11:10:00+00:00"),
        ("d6", "private:conv-1", "agent-1", "agent-1", "自言自语", "2026-01-02T11:20:00+00:00"),
    ]
    chat.executemany(
        """
        INSERT INTO t_nexus_peer_deliveries (
            id, scope, sender_agent_id, target_agent_id, message_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    chat.commit()
    async with sessions() as session:
        snapshot = await nebula.collect_snapshot(session, chat, now=now)
        detail = await nebula.collect_collaborations(
            session, chat, owner_id="u1", now=now
        )

    edges = snapshot["collaborationEdges"]
    assert len(edges) == 1
    edge = edges[0]
    assert edge["requestCount"] == 3
    assert edge["recent"] is True
    assert edge["lastRequestedAt"] == "2026-01-02T11:00:00+00:00"
    assert len(edge["directions"]) == 2
    assert all("references" in item for item in edge["directions"])
    assert not any(
        reference["kind"] == "conversation"
        for item in edge["directions"]
        for reference in item["references"]
    )
    assert snapshot["dataWarnings"][0]["code"] == "COLLABORATION_UNMAPPED"
    assert snapshot["dataWarnings"][0]["count"] == 2

    owned = detail["items"][0]
    conversation_refs = [
        reference["id"]
        for item in owned["directions"]
        for reference in item["references"]
        if reference["kind"] == "conversation"
    ]
    assert set(conversation_refs) == {"conv-1"}


@pytest.mark.asyncio
async def test_employee_tasks_pagination_and_link_task_counts(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    _insert_employee(chat, "e2", "星二", "agent-2")
    now = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    async with sessions() as session:
        await _worker(session, "w1", "e1")
        await _worker(session, "w2", "e2")
        session.add_all(
            [
                _task("a", status="RUNNING", owner="e1", worker="w1", title="A"),
                _task("b", status="RUNNING", owner="e1", worker="w1", title="B"),
                _task("c", status="WAITING", owner="e1", worker="w1", title="C"),
                _task("d", status="FAILED", owner="e1", title="D"),
                _task("e", status="RUNNING", owner="e2", worker="w2", title="E"),
                _task("f", status="RUNNING", owner="e1", worker="w2", title="F"),
                _task("g", status="RUNNING", owner="e1", worker="w1", title="G", priority="HIGH"),
            ]
        )
        await session.commit()

        page = await nebula.collect_employee_tasks(
            session, chat, "e1", task_status="RUNNING", page=1, page_size=2, now=now
        )
        link = await nebula.collect_link_tasks(session, chat, "w1", now=now)
        with pytest.raises(ValueError):
            await nebula.collect_link_tasks(session, chat, "nope", now=now)

    assert page["total"] == 4
    assert len(page["items"]) == 2
    conflict = next(item for item in page["items"] if item["id"] == "f")
    assert conflict["conflict"] is True
    assert "TASK_OWNER_CONFLICT" in conflict["conflictCodes"]
    assert link["counts"] == {"RUNNING": 3, "WAITING": 1}
    assert {item["id"] for item in link["items"]} == {"a", "b", "c", "g"}

    high = await nebula.collect_employee_tasks(
        session, chat, "e1", priority="HIGH", page=1, page_size=10, now=now
    )
    assert high["total"] == 1
    assert high["items"][0]["id"] == "g"


@pytest.mark.asyncio
async def test_snapshot_stays_readable_when_platform_source_degrades(facts, monkeypatch):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")

    async def broken(_session):
        raise SQLAlchemyError("platform unavailable")

    monkeypatch.setattr(nebula, "_load_workers", broken)
    now = datetime(2026, 1, 2, 0, 30, tzinfo=UTC)
    async with sessions() as session:
        snapshot = await nebula.collect_snapshot(session, chat, now=now)

    assert snapshot["employees"][0]["id"] == "e1"
    assert snapshot["workers"] == []
    assert "workers" in snapshot["degradedSources"]
    assert "tasks" in snapshot["degradedSources"]
    assert snapshot["totals"]["employees"] == 1


@pytest.mark.asyncio
async def test_http_contract_uses_camel_case_and_validates_status(facts):
    sessions, chat = facts
    _insert_employee(chat, "e1", "星一", "agent-1")
    async with sessions() as session:
        await _worker(session, "w1", "e1")
        session.add(_task("a", status="RUNNING", owner="e1", worker="w1"))
        await session.commit()

    app = FastAPI()
    app.include_router(nebula.router)

    async def session_override():
        async with sessions() as session:
            yield session

    def chat_override():
        yield chat

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[nebula.chat_database] = chat_override
    app.dependency_overrides[nebula.require_owner] = lambda: "u1"

    with TestClient(app) as client:
        snapshot = client.get("/api/v1/nebula/snapshot")
        assert snapshot.status_code == 200
        assert snapshot.json()["employees"][0]["counts"]["running"] == 1
        assert client.get("/api/v1/nebula/employees/e1/tasks").status_code == 200
        assert (
            client.get(
                "/api/v1/nebula/employees/e1/tasks", params={"priority": "HIGH"}
            ).json()["total"]
            == 0
        )
        bad = client.get("/api/v1/nebula/employees/e1/tasks", params={"status": "BROKEN"})
        assert bad.status_code == 422
        assert client.get("/api/v1/nebula/employees/missing").status_code == 404
        assert client.get("/api/v1/nebula/links/w1/tasks").json()["counts"] == {"RUNNING": 1}
        assert client.get("/api/v1/nebula/tasks/a").json()["task"]["id"] == "a"
        collaborations = client.get("/api/v1/nebula/collaborations")
        assert collaborations.status_code == 200
        assert "items" in collaborations.json()
