"""v1.5.0 最终结构、Task 独占、原生执行、认证代次、重发和复盘回归。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import TaskStatus, WorkerStatus
from app.executors.errors import OpenCodeSessionNotFound
from app.models import (
    AgentWorker,
    Base,
    ExperienceReviewJob,
    Task,
    TaskCategory,
    WorkerMaintenanceOperation,
)
from app.runtime.dispatcher import Dispatcher
from app.runtime.lifecycle import RuntimeService, RuntimeStateError
from app.runtime.maintenance import (
    confirm_maintenance_stopped,
    recover_expired,
    submit_operation,
)
from app.runtime.occupancy import occupying_clause
from app.runtime.runtime_context import RuntimeContextError, generate_runtime_context, resolve_runtime_context
from app.schemas import TaskCreate, TaskRead, WorkerRead
from app.services import TaskService


class Native:
    def __init__(self):
        self.sessions = {}
        self.sent = []
        self.bodies = []
        self.questions_data = []
        self.permissions_data = []
        self.fail_send = False
        self.stop_fails = False
        self.creates = 0
        self.aborts = 0
        self.pause_send = None

    async def health(self):
        return {"version": "1.18.35"}

    async def create_session(self, directory, **kwargs):
        self.creates += 1
        sid = f"ses_{self.creates}"
        self.sessions[sid] = {"status": "idle", "messages": []}
        return {"id": sid}

    async def status(self, directory, sid):
        if sid not in self.sessions:
            raise OpenCodeSessionNotFound(sid)
        return self.sessions[sid]["status"]

    async def children(self, directory, sid):
        if sid not in self.sessions:
            raise OpenCodeSessionNotFound(sid)
        return []

    async def questions(self, directory):
        return self.questions_data

    async def permissions(self, directory):
        return self.permissions_data

    async def abort(self, directory, sid):
        self.aborts += 1
        if self.stop_fails:
            raise ConnectionError("offline")
        if sid not in self.sessions:
            raise OpenCodeSessionNotFound(sid)
        self.sessions[sid]["status"] = "idle"
        return True

    async def messages_page(self, directory, sid, **kwargs):
        return {"messages": list(reversed(self.sessions[sid]["messages"])), "next_before": None}

    async def request(self, method, path, **kwargs):
        if path.endswith(("/prompt_async", "/command")):
            self.bodies.append(kwargs["body"])
            if self.pause_send:
                await self.pause_send.wait()
            if self.fail_send:
                if isinstance(self.fail_send, Exception):
                    raise self.fail_send
                raise TimeoutError("unknown acceptance")
            sid = path.split("/")[2]
            self.sent.append((sid, kwargs["body"]["messageID"]))
            self.sessions[sid]["messages"].append(
                {"info": {"id": kwargs["body"]["messageID"], "role": "user"}, "parts": []}
            )
            self.sessions[sid]["status"] = "busy"
            return None
        raise AssertionError(path)


@pytest.fixture
async def setup_runtime(monkeypatch, tmp_path):
    from app.runtime import lifecycle, workspace_changes

    key = f"worker-{uuid4().hex[:12]}"
    category = f"cat-{uuid4().hex[:12]}"
    native = Native()

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    async def prepare(*args, **kwargs):
        return str(tmp_path)

    async def materialize(worker, workspace, attachments):
        return attachments

    async def head(*args):
        return None

    monkeypatch.setattr(lifecycle, "connect", connect)
    monkeypatch.setattr(lifecycle, "prepare_workspace", prepare)
    monkeypatch.setattr(lifecycle, "materialize_task_attachments", materialize)
    monkeypatch.setattr(workspace_changes, "workspace_head_commit", head)
    monkeypatch.setattr(get_settings(), "gateway_master_key", "v150-test-signing-key")
    monkeypatch.setattr(get_settings(), "experience_review_enabled", False)
    async with SessionLocal() as session:
        worker = AgentWorker(
            id=key,
            name="Worker",
            mode="LINUX_REMOTE",
            host="worker.invalid",
            username="root",
            workspace_root="/workspace",
            status="IDLE",
            category_ids=[category],
        )
        session.add_all([worker, TaskCategory(id=category, name=category)])
        await session.commit()
    return key, category, native


async def create_task(session, setup_runtime, **kwargs):
    worker, category, _ = setup_runtime
    return await TaskService(session).create(
        TaskCreate(
            task_type="ISSUE",
            category_id=category,
            title="修复问题",
            description="完成修复及验证",
            agent_worker_id=worker,
            **kwargs,
        )
    )


def test_target_schema_has_no_execution_history():
    tables = Base.metadata.tables
    assert len(tables["t_nexus_tasks"].columns) == 27
    assert len(tables["t_nexus_agent_workers"].columns) == 25
    assert "t_nexus_worker_commands" not in tables
    assert "t_nexus_worker_http_slots" not in tables
    assert len(tables["t_nexus_worker_maintenance_operations"].columns) == 24
    assert not {
        "t_nexus_runtime_sessions",
        "t_nexus_worker_instances",
        "t_nexus_opencode_execution_environments",
        "t_nexus_worker_connections",
    } & set(tables)
    assert {s.value for s in TaskStatus} == {
        "PENDING",
        "RUNNING",
        "WAITING",
        "SUCCEEDED",
        "CANCELED",
        "FAILED",
    }
    assert {s.value for s in WorkerStatus} == {"OFFLINE", "IDLE", "WORKING", "PAUSED"}


async def test_same_source_can_create_multiple_tasks(setup_runtime):
    async with SessionLocal() as db:
        one = await create_task(db, setup_runtime, source="group_chat", external_task_id="group")
        two = await create_task(db, setup_runtime, source="group_chat", external_task_id="group")
        assert one.id != two.id


async def test_native_round_then_manual_completion(setup_runtime):
    worker, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        assert task.status == "RUNNING" and not task.pending_prompt
        assert len(native.sent) == 1
        native.sessions[task.current_session_id]["status"] = "idle"
        task = await runtime.refresh(task.id)
        assert task.status == "WAITING"
        assert (await db.get(AgentWorker, worker)).status == "WORKING"
        assert await db.scalar(select(Task.id).where(occupying_clause(worker))) == task.id
        task = await runtime.confirm_completion(task.id)
        assert task.status == "SUCCEEDED" and task.finished_at
        assert (await db.get(AgentWorker, worker)).status == "IDLE"


@pytest.mark.parametrize("kind", ["question", "permission"])
async def test_native_asks_occupy_worker(setup_runtime, kind):
    worker, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        setattr(native, kind + "s_data", [{"id": "ask", "sessionID": task.current_session_id}])
        task = await runtime.refresh(task.id)
        assert (task.status, task.waiting_reason) == ("WAITING", kind)
        assert (await db.get(AgentWorker, worker)).status == "WORKING"
        with pytest.raises(RuntimeStateError):
            await runtime.confirm_completion(task.id)


async def test_unknown_submission_retries_same_task_and_session(setup_runtime):
    _, _, native = setup_runtime
    native.fail_send = True
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        sid, outbound = task.current_session_id, task.pending_message_id
        assert task.pending_prompt and sid
        native.fail_send = False
        task = await runtime.refresh(task.id)
        assert task.current_session_id == sid and task.last_message_id == outbound
        assert not task.pending_prompt and native.creates == 1


async def test_unknown_submission_waits_for_busy_native(setup_runtime):
    _, _, native = setup_runtime
    native.fail_send = True
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        native.sessions[task.current_session_id]["status"] = "busy"
        native.fail_send = False
        await runtime.retry_submission(task.id)
        assert not native.sent and task.pending_message_id
        native.sessions[task.current_session_id]["status"] = "idle"
        await runtime.retry_submission(task.id)
        assert len(native.sent) == 1


async def test_retry_submission_losing_race_returns_serializable_task(setup_runtime, monkeypatch):
    _, _, native = setup_runtime
    native.fail_send = True
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        task_id, outbound = task.id, task.pending_message_id
        native.fail_send = False
        lock_task = runtime.lock_task
        locks = 0

        async def lock_after_competing_retry(task_id):
            nonlocal locks
            locks += 1
            if locks == 2:
                # 提交已持久化、再次加锁前，另一消费者先完成同一条待提交消息。
                async with SessionLocal() as other:
                    await RuntimeService(other).retry_submission(task_id)
            return await lock_task(task_id)

        monkeypatch.setattr(runtime, "lock_task", lock_after_competing_retry)
        result = await runtime.retry_submission(task_id)
        response = TaskRead.model_validate(result)
        assert response.id == task_id
        assert response.last_message_id == outbound
        assert response.pending_message_id is None
        assert response.status == "RUNNING"
        assert native.sent == [(response.current_session_id, outbound)]


async def test_missing_native_session_waits_for_manual_restart(setup_runtime):
    _, _, native = setup_runtime
    native.fail_send = True
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        old = task.current_session_id
        native.sessions.pop(old)
        native.fail_send = False
        task = await runtime.refresh(task.id)
        assert task.current_session_id == old and native.creates == 1
        assert task.status == "WAITING" and task.waiting_reason == "session_missing"
        task = await runtime.restart(task.id)
        assert task.current_session_id != old and native.creates == 2
        assert task.pending_prompt is None
        contexts = [part for part in native.bodies[-1]["parts"] if part.get("synthetic")]
        assert len(contexts) == 1
        token = contexts[0]["text"].split("\n")[0].removeprefix("ASTRA_RUNTIME_CONTEXT: ")
        assert (await resolve_runtime_context(db, token)).session_id == task.current_session_id


async def test_offline_keeps_worker_and_task_occupancy(setup_runtime):
    worker, _, native = setup_runtime
    native.fail_send = True
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        row = await db.get(AgentWorker, worker)
        row.status = "OFFLINE"
        await db.commit()
        await runtime.refresh(task.id)
        assert task.agent_worker_id == worker and task.pending_prompt
        assert await db.scalar(select(Task.id).where(occupying_clause(worker))) == task.id


async def test_cancel_requires_confirmed_stop(setup_runtime):
    worker, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        native.stop_fails = True
        with pytest.raises(ConnectionError):
            await runtime.stop(task.id)
        task_id = task.id
        await db.rollback()
        task = await db.get(Task, task_id)
        assert task.status == "RUNNING"
        assert await db.scalar(select(Task.id).where(occupying_clause(worker))) == task.id
        native.stop_fails = False
        task = await runtime.stop(task.id)
        assert task.status == "CANCELED" and not task.pending_prompt
        assert (await db.get(AgentWorker, worker)).status == "IDLE"


async def test_force_stop_audited_and_releases(setup_runtime):
    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        task = await runtime.confirm_execution_stop(task.id, operator="human", reason="已在目标主机停止进程")
        assert task.status == "CANCELED" and task.execution_released


async def test_waiting_and_offline_workers_cannot_claim_second_task(setup_runtime):
    worker, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        native.sessions[task.current_session_id]["status"] = "idle"
        await runtime.refresh(task.id)
        second = await create_task(db, setup_runtime)
        second_id = second.id
        claimed = await Dispatcher(db).claim_once()
        await db.refresh(second)
        assert claimed is None or claimed.id != second_id
        assert second.status == "PENDING"


async def test_context_is_signed_and_rejects_previous_session(setup_runtime):
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task.current_session_id = "ses-old"
        task.workspace = "/workspace/task"
        token = generate_runtime_context(task)
        assert (await resolve_runtime_context(db, token)).session_id == "ses-old"
        task.current_session_id = "ses-new"
        await db.commit()
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(db, token)
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(db, token + "tamper")


async def test_confirmation_freezes_review_sources(setup_runtime, monkeypatch):
    worker, _, native = setup_runtime
    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        native.sessions[task.current_session_id]["status"] = "idle"
        await runtime.refresh(task.id)
        await runtime.confirm_completion(task.id)
        job = await db.scalar(select(ExperienceReviewJob).where(ExperienceReviewJob.task_id == task.id))
        assert job.source_snapshot["agent_worker_id"] == worker
        assert job.source_snapshot["root_session_id"] == task.current_session_id
        assert job.source_snapshot["workspace"] == task.workspace


async def test_worker_secret_fields_are_not_serialized(setup_runtime):
    worker, _, _ = setup_runtime
    async with SessionLocal() as db:
        row = await db.get(AgentWorker, worker)
        row.ssh_password = "encrypted"
        row.git_token_ciphertext = "encrypted"
        body = WorkerRead.model_validate(row).model_dump()
        assert not {"ssh_password", "git_token_ciphertext", "token_digest"} & set(body)
        assert body["has_ssh_password"] and body["has_git_token"]


async def test_maintenance_loss_does_not_release_remote_lock(setup_runtime):
    worker, _, _ = setup_runtime
    async with SessionLocal() as db:
        operation = WorkerMaintenanceOperation(
            id="op_" + uuid4().hex,
            agent_worker_id=worker,
            active_worker_id=worker,
            action="deploy",
            state="RUNNING",
            lease_owner="lost",
            lease_until=datetime.now(UTC) - timedelta(seconds=2),
        )
        db.add(operation)
        await db.commit()
        await recover_expired()
        await db.refresh(operation)
        assert operation.state == "FAILED" and operation.active_worker_id == worker
        operation_id = operation.id
        with pytest.raises(HTTPException):
            await submit_operation(db, worker, "check")
        await db.rollback()
        await confirm_maintenance_stopped(db, operation_id, "human")
        await db.refresh(operation)
        assert operation.active_worker_id is None


async def test_cancel_during_start_does_not_reopen_or_reclaim_worker(setup_runtime, monkeypatch):
    worker_id, _, native = setup_runtime
    async with SessionLocal() as db:
        original = await create_task(db, setup_runtime)
        replacement = await create_task(db, setup_runtime)
    real_project = RuntimeService.project

    async def cancel_after_claim(self, task, status, *, detail=None):
        result = await real_project(self, task, status, detail=detail)
        if task.id == original.id and detail == "准备工作区":
            async with SessionLocal() as concurrent:
                stopped = await RuntimeService(concurrent).stop(original.id)
                assert stopped.status == "CANCELED"
                assert (await concurrent.get(AgentWorker, worker_id)).status == "IDLE"
                started = await RuntimeService(concurrent).start(replacement.id)
                assert started.status == "RUNNING"
        return result

    monkeypatch.setattr(RuntimeService, "project", cancel_after_claim)
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        stopped = await runtime.start(original.id)
        assert stopped.status == "CANCELED" and not stopped.workspace and not stopped.current_session_id
        assert native.creates == 1 and len(native.sent) == 1
        occupied = await db.scalars(select(Task.id).where(occupying_clause(worker_id)))
        assert occupied.all() == [replacement.id]
        with pytest.raises(RuntimeStateError, match="任务已结束"):
            await runtime.start(original.id)


async def test_old_start_does_not_continue_after_same_task_is_restarted(setup_runtime, monkeypatch):
    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task_id = (await create_task(db, setup_runtime)).id

    original_claimed = asyncio.Event()
    new_claimed = asyncio.Event()
    finish_original = asyncio.Event()
    finish_new = asyncio.Event()
    real_project = RuntimeService.project

    async def suspend_after_claim(self, task, status, *, detail=None):
        result = await real_project(self, task, status, detail=detail)
        if detail == "准备工作区":
            if not original_claimed.is_set():
                original_claimed.set()
                await finish_original.wait()
            else:
                new_claimed.set()
                await finish_new.wait()
        return result

    async def start(*, restart=False):
        async with SessionLocal() as db:
            return await RuntimeService(db).start(task_id, restart=restart)

    monkeypatch.setattr(RuntimeService, "project", suspend_after_claim)
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        original = group.create_task(start())
        await original_claimed.wait()
        async with SessionLocal() as db:
            assert (await RuntimeService(db).stop(task_id)).status == "CANCELED"
        replacement = group.create_task(start(restart=True))
        await new_claimed.wait()
        finish_original.set()
        assert not (await original).workspace
        assert not native.sent
        finish_new.set()
        assert (await replacement).current_session_id
        assert len(native.sent) == 1


async def test_resume_with_native_session_requires_actual_action(setup_runtime):
    import httpx

    from app.main import app
    from app.task_tools import TaskTools

    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        task_id, sid = task.id, task.current_session_id
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/api/tasks/{task_id}/resume")
        assert response.status_code == 409
        assert "补充输入" in response.json()["detail"]

    async with SessionLocal() as db:
        result = await TaskTools(db).resume(task_id)
        assert result["ok"] is False and "补充输入" in result["error"]
        task = await db.get(Task, task_id)
        assert task.current_session_id == sid and task.waiting_reason == "confirmation"
        assert len(native.sent) == 1 and task.status == "WAITING"


async def test_resume_retries_start_without_native_session(setup_runtime):
    from app.task_tools import TaskTools

    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task.status, task.waiting_reason = "WAITING", "start_error"
        await db.commit()
        result = await TaskTools(db).resume(task.id)
        assert result["ok"] is True
        assert task.current_session_id and task.status == "RUNNING"
        assert len(native.sent) == 1


async def test_signed_context_is_fully_redacted(setup_runtime):
    from app.runtime.redaction import redact_text

    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task.current_session_id = "ses-secret"
        task.workspace = "/workspace/task"
        token = generate_runtime_context(task)
        assert redact_text(token) == "[已隐藏]"


async def test_child_execution_blocks_confirmation_and_is_stopped_on_cancel(setup_runtime, monkeypatch):
    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        root = task.current_session_id
        native.sessions[root]["status"] = "idle"
        native.sessions["ses_child"] = {"status": "busy", "messages": []}

        async def children(directory, sid):
            return [{"id": "ses_child"}] if sid == root else []

        monkeypatch.setattr(native, "children", children)
        await runtime.refresh(task.id)
        assert task.status == "RUNNING"
        task.status = "WAITING"
        await db.commit()
        task_id = task.id
        with pytest.raises(RuntimeStateError):
            await runtime.confirm_completion(task_id)
        await db.rollback()
        task = await runtime.stop(task_id)
        assert task.status == "CANCELED"
        assert native.sessions["ses_child"]["status"] == "idle"


async def test_review_reads_frozen_native_source_and_writes_real_candidate(setup_runtime, monkeypatch):
    from app.models import ExperienceEvidence, KnowledgeItem
    from app.runtime.experience_review import trace_reader
    from app.runtime.experience_review.candidate_writer import write_candidates
    from app.runtime.experience_review.evidence import validate_candidate
    from app.runtime.experience_review.schemas import Candidate

    _, _, native = setup_runtime
    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)

    monkeypatch.setattr(trace_reader, "OpenCodeServerClient", lambda *args, **kwargs: native)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        sid = task.current_session_id
        native.sessions[sid]["messages"].append(
            {
                "info": {"id": "msg_verified", "role": "assistant"},
                "parts": [
                    {"id": "prt_verified", "type": "text", "text": "已读取部署配置并验证端口唯一来源。"}
                ],
            }
        )
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task.id)
        await runtime.confirm_completion(task.id)
        job = await db.scalar(select(ExperienceReviewJob).where(ExperienceReviewJob.task_id == task.id))
        trace = await trace_reader.read_trace(job.source_snapshot)
        assert trace.sessions == [sid] and trace.message_count == 2
        ref = next(event.ref for event in trace.events if event.message_id == "msg_verified")
        candidate = Candidate(
            title="端口来源核验", content_md="部署时从 Worker 本地配置读取监听端口。", evidence_refs=[ref]
        )
        validate_candidate(candidate, trace)
        ids = await write_candidates(db, job=job, trace=trace, candidates=[candidate])
        await db.commit()
        item = await db.get(KnowledgeItem, ids[0])
        assert item.status == "candidate" and item.source_session_id == sid
        evidence = await db.scalar(
            select(ExperienceEvidence).where(ExperienceEvidence.knowledge_id == item.id)
        )
        assert evidence.native_session_id == sid and evidence.review_job_id == job.id
        assert evidence.message_id == "msg_verified" and evidence.validation_status == "validated"
        with pytest.raises(trace_reader.TraceUnavailable):
            await trace_reader.read_trace({**job.source_snapshot, "source_session_ids": [sid, "ses_foreign"]})
        native.sessions.pop(sid)
        with pytest.raises(trace_reader.TraceUnavailable):
            await trace_reader.read_trace(job.source_snapshot)


async def test_task_delete_protects_active_review_and_keeps_completed_evidence(setup_runtime, monkeypatch):
    import httpx

    from app.main import app
    from app.models import ExperienceEvidence, KnowledgeItem
    from app.runtime.experience_review import coordinator

    _, _, native = setup_runtime
    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        await runtime.start(task.id)
        sid = task.current_session_id
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task.id)
        await runtime.confirm_completion(task.id)
        task_id = task.id
        job = await db.scalar(select(ExperienceReviewJob).where(ExperienceReviewJob.task_id == task_id))
        job_id = job.id
        snapshot = job.source_snapshot
        item = KnowledgeItem(
            id="knowledge-" + uuid4().hex,
            type="experience",
            title="已有经验候选",
            status="candidate",
            source_task_id=task_id,
            source_session_id=sid,
            created_by="fixture",
            updated_by="fixture",
        )
        evidence = ExperienceEvidence(
            knowledge_id=item.id,
            task_id=task_id,
            source_session_id=sid,
            native_session_id=sid,
            evidence_type="verification",
            review_job_id=job_id,
        )
        job.status = coordinator.FAILED
        job.candidate_ids = [item.id]
        db.add_all([item, evidence])
        await db.commit()
        item_id, evidence_id = item.id, evidence.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/tasks/{task_id}"
        async with SessionLocal() as retry_db:
            retry_ready = asyncio.Event()
            delete_started = asyncio.Event()
            commit_retry = asyncio.Event()
            commit = retry_db.commit
            lock_source = coordinator.lock_review_source_task

            async def pause_retry_commit():
                await retry_db.flush()
                retry_ready.set()
                await commit_retry.wait()
                await commit()

            async def observe_delete_lock(session, source_task_id):
                if session is not retry_db:
                    delete_started.set()
                await lock_source(session, source_task_id)

            with monkeypatch.context() as patch:
                patch.setattr(retry_db, "commit", pause_retry_commit)
                patch.setattr(coordinator, "lock_review_source_task", observe_delete_lock)
                async with asyncio.TaskGroup() as group:
                    retried = group.create_task(coordinator.retry_job(retry_db, job_id))
                    await asyncio.wait_for(retry_ready.wait(), timeout=5)
                    deleting = group.create_task(client.delete(path))
                    await asyncio.wait_for(delete_started.wait(), timeout=5)
                    commit_retry.set()
                assert retried.result().status == coordinator.PENDING
                assert deleting.result().status_code == 409

        for review_status in coordinator.ACTIVE_STATUSES:
            async with SessionLocal() as db:
                job = await db.get(ExperienceReviewJob, job_id)
                job.status = review_status
                job.lease_token = "fixture-lease"
                await db.commit()

            blocked = await client.delete(path)
            assert blocked.status_code == 409 and "未完成的经验复盘" in blocked.json()["detail"]
            async with SessionLocal() as db:
                job = await db.get(ExperienceReviewJob, job_id)
                assert job.status == review_status and job.lease_token == "fixture-lease"
                assert (await db.get(Task, task_id)).status == "SUCCEEDED"
                assert (await db.get(KnowledgeItem, item_id)).source_task_id == task_id
                assert (await db.get(ExperienceEvidence, evidence_id)).task_id == task_id

        async with SessionLocal() as db:
            job = await db.get(ExperienceReviewJob, job_id)
            job.status = coordinator.SUCCEEDED
            job.lease_token = None
            await db.commit()

        deleted = await client.delete(path)
        assert deleted.status_code == 200 and deleted.json() == {"deleted": task_id}
        assert (await client.get(path)).status_code == 404

    async with SessionLocal() as db:
        job = await db.get(ExperienceReviewJob, job_id)
        assert job.status == coordinator.SUCCEEDED and job.source_snapshot == snapshot
        assert job.candidate_ids == [item_id]
        assert (await db.get(KnowledgeItem, item_id)).source_task_id is None
        evidence = await db.get(ExperienceEvidence, evidence_id)
        assert evidence.task_id is None and evidence.native_session_id == sid


async def test_group_task_creation_freezes_the_original_human_and_employee(setup_runtime):
    from app.models import Event
    from nexus_api.db_models import discussions

    channel = "channel_" + uuid4().hex[:12]
    async with SessionLocal() as db:
        await db.execute(
            discussions.insert().values(
                id="discussion_" + uuid4().hex[:12],
                channel_id=channel,
                message_id=uuid4().hex,
                initiator_id="original-human",
                generation=1,
                state="running",
                participants_json='["original-employee"]',
                active_employee_id="original-employee",
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        await db.commit()
        task = await create_task(
            db,
            setup_runtime,
            source="group_chat",
            external_task_id=channel,
            origin_employee_id="original-employee",
        )
        event = await db.scalar(
            select(Event).where(Event.task_id == task.id, Event.event_type == "group_task_requested")
        )
        assert event.detail["initiator_id"] == "original-human"
        unrelated = await create_task(
            db,
            setup_runtime,
            source="group_chat",
            external_task_id=channel,
            origin_employee_id="other-employee",
        )
        assert (
            await db.scalar(
                select(Event).where(Event.task_id == unrelated.id, Event.event_type == "group_task_requested")
            )
            is None
        )


async def test_maintenance_actor_comes_from_the_request_identity(setup_runtime):
    import httpx

    from app.main import app

    worker, _, _ = setup_runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.post(f"/api/workers/{worker}/maintenance", json={"action": "check"})
        ).status_code == 401
        response = await client.post(
            f"/api/workers/{worker}/maintenance",
            headers={"X-Owner-Id": "actual-human"},
            json={"action": "check", "requested_by": "spoofed-actor"},
        )
        assert response.status_code == 202
        assert response.json()["requested_by"] == "actual-human"


@pytest.mark.parametrize("unknown_error", [TimeoutError("unknown"), ConnectionError("SSH exit unknown")])
async def test_unknown_mutating_maintenance_keeps_worker_locked(setup_runtime, monkeypatch, unknown_error):
    from app.runtime import maintenance

    worker, _, _ = setup_runtime

    async def unknown(*args, **kwargs):
        raise unknown_error

    monkeypatch.setattr(maintenance, "run_worker_operation", unknown)
    async with SessionLocal() as db:
        row = await db.get(AgentWorker, worker)
        row.repository_url = "https://example.test/repository.git"
        await db.commit()
        operation = await submit_operation(db, worker, "update")
        operation_id = operation.id
    assert await maintenance.run_maintenance_once("maintenance-test")
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        assert operation.state == "FAILED" and operation.stage == "stop_unconfirmed"
        assert operation.active_worker_id == worker
        with pytest.raises(HTTPException, match="请先确认远端维护已停止") as exc:
            await maintenance.retry_operation(db, operation_id)
        assert exc.value.status_code == 409
        with pytest.raises(HTTPException):
            await submit_operation(db, worker, "check")


async def test_health_probe_reads_the_current_worker_without_instance_aliases(setup_runtime, monkeypatch):
    from app.runtime.worker_health import probe_online_worker

    worker_id, _, _ = setup_runtime

    async def inspect(worker, name, body):
        assert worker.id == worker_id and name == "inspect_runtime"
        assert body["expected_opencode_version"] == "1.18.35"
        return {"opencode": {"status": "healthy"}, "spec_revision": "verified-source"}

    monkeypatch.setattr("app.runtime.worker_health.run_worker_operation", inspect)

    async def connection(*args):
        from types import SimpleNamespace

        return SimpleNamespace(observation=None)

    monkeypatch.setattr("app.runtime.worker_health.connections.get", connection)
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        await db.commit()
        result = await probe_online_worker(worker, {"opencode": {"version": "1.18.35"}})
        assert result["spec_revision"] == "verified-source"
        assert result["opencode"]["status"] == "healthy"
        assert result["model"]["status"] == "unknown"


async def test_local_debug_check_uses_authenticated_native_http(setup_runtime, monkeypatch):
    import base64

    import httpx

    from app.runtime import maintenance

    worker_id, _, _ = setup_runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "executor_type", "opencode_server")
    monkeypatch.setattr(settings, "opencode_server_username", "local-user")
    monkeypatch.setattr(settings, "opencode_server_password", "local-test-secret")
    auth = "Basic " + base64.b64encode(b"local-user:local-test-secret").decode()
    requests = []

    def native(request):
        requests.append(request.url.path)
        assert request.headers["Authorization"] == auth
        return httpx.Response(200, json={"healthy": True, "version": settings.opencode_server_version})

    real_client = httpx.AsyncClient

    def client(**kwargs):
        return real_client(**kwargs, transport=httpx.MockTransport(native))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        worker.mode = "LOCAL"
        await db.commit()
        operation = await submit_operation(db, worker_id, "check")
        operation_id = operation.id
    assert await maintenance.run_maintenance_once("local-check")
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        assert operation.state == "SUCCEEDED"
        assert operation.after_snapshot["scope"] == "local_opencode_health"
        assert requests == ["/global/health"]


async def test_confirmed_native_rejection_waits_without_retrying_or_releasing_worker(setup_runtime):
    from app.executors.errors import OpenCodeExecutionError

    worker, _, native = setup_runtime
    native.fail_send = OpenCodeExecutionError("OpenCode HTTP 400: invalid prompt")
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        assert task.status == "WAITING" and task.waiting_reason == "input_rejected"
        assert task.pending_message_id is None and task.pending_prompt is None
        assert (await db.get(AgentWorker, worker)).status == "WORKING"
        await runtime.refresh(task.id)
        assert len(native.bodies) == 1
        assert task.waiting_reason == "input_rejected"


async def test_admin_lists_and_native_workspace_binding(setup_runtime, monkeypatch, tmp_path):
    import httpx

    from app.api import workspace_routes
    from app.main import app

    worker_id, _, _ = setup_runtime
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        worker.name = f"研发 Worker {worker_id}"
        name = worker.name
        task = await create_task(db, setup_runtime)
        task.current_session_id = "ses_admin_test"
        task.workspace = str(tmp_path)
        task_id = task.id
        db.add_all(
            [
                WorkerMaintenanceOperation(
                    id=f"{worker_id}-1", agent_worker_id=worker_id, action="deploy", state="SUCCEEDED"
                ),
                WorkerMaintenanceOperation(
                    id=f"{worker_id}-2", agent_worker_id=worker_id, action="deploy", state="SUCCEEDED"
                ),
                WorkerMaintenanceOperation(
                    id=f"{worker_id}-3", agent_worker_id=worker_id, action="check", state="FAILED"
                ),
            ]
        )
        await db.commit()

    class RunningOpenCode:
        async def request(self, method, path):
            assert (method, path) == ("GET", "/global/health")
            return {"healthy": True, "version": "1.18.35"}

    @asynccontextmanager
    async def connect(worker):
        assert worker.id == worker_id
        yield RunningOpenCode()

    monkeypatch.setattr("app.runtime.worker_connection.connections.online", lambda _: True)
    monkeypatch.setattr(workspace_routes, "connect", connect)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tasks = await client.get("/api/tasks", params={"q": task_id, "limit": 1})
        assert tasks.headers["X-Total-Count"] == "1"
        assert tasks.json()[0]["worker_name"] == name
        assert (await client.get(f"/api/tasks/{task_id}")).json()["worker_name"] == name
        workers = await client.get("/api/workers", params={"q": name, "status": "IDLE", "limit": 1})
        assert workers.headers["X-Total-Count"] == "1"
        assert workers.json()[0]["id"] == worker_id
        for offset in (0, 1):
            history = await client.get(
                f"/api/workers/{worker_id}/maintenance",
                params={
                    "action": "deploy",
                    "state": "SUCCEEDED",
                    "limit": 1,
                    "offset": offset,
                },
            )
            assert history.json()["total"] == 2
            assert history.json()["items"][0]["id"] == f"{worker_id}-{2 - offset}"
        binding = (await client.get(f"/api/tasks/{task_id}/workspace")).json()
        assert binding["sessions"] == [{"id": "ses_admin_test"}]
        assert binding["runtime"]["workspace_ui"] == "/opencode-workspace/1.18.35/index.html"

        async with SessionLocal() as db:
            worker = await db.get(AgentWorker, worker_id)
            worker.status = "OFFLINE"
            await db.commit()
        monkeypatch.setattr("app.runtime.worker_connection.connections.online", lambda _: False)
        binding = (await client.get(f"/api/tasks/{task_id}/workspace")).json()
        assert binding["runtime"]["workspace_ui"] == "/opencode-workspace/1.18.35/index.html"
        assert binding["runtime"]["workspace_ui_error"] is None

        from paramiko import SSHException

        @asynccontextmanager
        async def disconnected(worker):
            raise SSHException("SSH not connected")
            yield

        monkeypatch.setattr(workspace_routes, "connect", disconnected)
        response = await client.get(f"/api/tasks/{task_id}/workspace")
        assert response.status_code == 200
        assert response.json()["runtime"]["workspace_ui"] is None
        assert "无法读取 OpenCode 版本" in response.json()["runtime"]["workspace_ui_error"]
        monkeypatch.setattr(workspace_routes, "connect", connect)
        binding = (await client.get(f"/api/tasks/{task_id}/workspace")).json()
        assert binding["runtime"]["workspace_ui"] == "/opencode-workspace/1.18.35/index.html"


async def test_native_sse_and_periodic_reconciliation_drive_task_without_browser(setup_runtime, monkeypatch):
    from app.runtime import lifecycle, recovery

    _, _, native = setup_runtime
    events = asyncio.Queue()
    first_read = asyncio.Event()
    reads = []
    status = native.status

    async def native_status(directory, session_id):
        reads.append(session_id)
        first_read.set()
        return await status(directory, session_id)

    async def native_events(_directory, *, on_subscribed):
        on_subscribed()
        while True:
            yield await events.get()

    native.status = native_status
    native.events = native_events
    monkeypatch.setattr(recovery, "connect", lifecycle.connect)
    monkeypatch.setattr(recovery, "_SCAN_SECONDS", 0.02)

    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task = await RuntimeService(db).start(task.id)
        task_id, sid = task.id, task.current_session_id
        native.sessions[sid]["status"] = "idle"

    async def wait_reason(reason):
        async with asyncio.timeout(2):
            while True:
                async with SessionLocal() as db:
                    current = await db.get(Task, task_id)
                    if current.waiting_reason == reason:
                        return current
                await asyncio.sleep(0.01)

    watcher = asyncio.create_task(recovery.watch_session(task_id))
    try:
        await asyncio.wait_for(first_read.wait(), 2)
        assert (await wait_reason("confirmation")).status == "WAITING"
        await events.put({"type": "session.idle", "properties": {"sessionID": "old-session"}})
        await events.put({"type": "server.heartbeat"})
        await asyncio.sleep(0.09)
        assert reads and set(reads) == {sid}

        child = "ses_child"
        native.sessions[child] = {"status": "idle", "messages": []}
        children = native.children

        async def native_children(directory, session_id):
            return [{"id": child}] if session_id == sid else await children(directory, session_id)

        native.children = native_children
        native.questions_data = [{"id": "question", "sessionID": child}]
        await events.put({"type": "session.created", "properties": {"info": {"id": child, "parentID": sid}}})
        await events.put({"type": "question.asked", "properties": {"sessionID": child}})
        assert (await wait_reason("question")).status == "WAITING"

        native.sessions[sid]["status"] = "idle"
        # The root's child list can briefly include a child that has just disappeared.
        del native.sessions[child]
        await events.put({"type": "session.deleted", "properties": {"info": {"id": child}}})
        assert (await wait_reason("confirmation")).status == "WAITING"

        native.sessions[sid]["status"] = "busy"
        await events.put({"type": "server.connected"})
        assert (await wait_reason(None)).status == "RUNNING"
        native.sessions[sid]["status"] = "idle"
        await events.put({"type": "session.idle", "properties": {"sessionID": sid}})
        assert (await wait_reason("confirmation")).status == "WAITING"
        async with SessionLocal() as db:
            completed = await RuntimeService(db).confirm_completion(task_id)
            assert completed.status == "SUCCEEDED"
        await asyncio.wait_for(watcher, 2)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_event_reconciliation_skips_a_replaced_binding(setup_runtime):
    from app.runtime.session_connection import task_session_binding

    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        old = task_session_binding(task)
        task = await runtime.start(task.id, restart=True)
        native.sessions[task.current_session_id]["status"] = "idle"
        result = await runtime.refresh(task.id, expected_binding=old)
        assert result.status == "RUNNING"
        result = await runtime.refresh(task.id, expected_binding=task_session_binding(task))
        assert result.status == "WAITING"


async def test_supervisor_retries_pending_submission_without_an_sse_event(setup_runtime, monkeypatch):
    from app.runtime import lifecycle, recovery

    _, _, native = setup_runtime
    native.fail_send = True

    async def silent_events(_directory, *, on_subscribed):
        await asyncio.Future()
        yield {}

    native.events = silent_events
    monkeypatch.setattr(recovery, "connect", lifecycle.connect)
    monkeypatch.setattr(recovery, "_SCAN_SECONDS", 0.02)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task = await RuntimeService(db).start(task.id)
        task_id, sid = task.id, task.current_session_id
        assert task.pending_message_id
    native.fail_send = False
    watcher = asyncio.create_task(recovery.watch_session(task_id))
    try:
        async with asyncio.timeout(2):
            while True:
                async with SessionLocal() as db:
                    current = await db.get(Task, task_id)
                    if not current.pending_message_id:
                        assert current.current_session_id == sid and len(native.sent) == 1
                        break
                await asyncio.sleep(0.01)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize("kind", ["question", "permission"])
async def test_confirm_api_and_mcp_recheck_unfinished_native_interaction(setup_runtime, kind):
    import httpx

    from app.main import app
    from app.task_tools import TaskTools

    worker_id, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await create_task(db, setup_runtime)
        task = await runtime.start(task.id)
        task_id, sid = task.id, task.current_session_id
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task_id)
        assert task.waiting_reason == "confirmation"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        setattr(native, kind + "s_data", [{"id": "ask", "sessionID": sid}])
        result = await client.post(f"/api/tasks/{task_id}/confirm")
        assert result.status_code == 409
        assert "原生执行、提问或权限请求" in result.json()["detail"]
        async with SessionLocal() as db:
            refused = await TaskTools(db).confirm(task_id)
            assert not refused["ok"] and "原生执行、提问或权限请求" in refused["error"]
            await RuntimeService(db).refresh(task_id)
        result = await client.post(f"/api/tasks/{task_id}/confirm")
        assert result.status_code == 409 and "等待人工确认" in result.json()["detail"]

        setattr(native, kind + "s_data", [])
        async with SessionLocal() as db:
            await RuntimeService(db).refresh(task_id)
            assert (await TaskTools(db).confirm(task_id))["status"] == "SUCCEEDED"
        assert (await client.get(f"/api/workers/{worker_id}")).json()["occupied_tasks"] == []


async def test_worker_console_reports_all_occupancy_independently_of_connection(setup_runtime, monkeypatch):
    import httpx

    from app.main import app
    from app.schemas import WorkerWrite

    worker_id, _, _ = setup_runtime
    from app.runtime.worker_connection import connections

    connected = set()
    monkeypatch.setattr(connections, "observation", lambda _: None)
    monkeypatch.setattr(connections, "online", lambda worker_id: worker_id in connected)
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        worker.status, worker.enabled = "OFFLINE", False
        expected = []
        for state in ("PENDING", "RUNNING", "WAITING", "SUCCEEDED", "CANCELED"):
            task = await create_task(db, setup_runtime)
            task.status = state
            task.title = f"任务 {state}"
            if state in {"RUNNING", "WAITING"}:
                expected.append({"id": task.id, "title": task.title})
            await db.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        root = f"/api/workers/{worker_id}"
        worker_row = (await client.get(root)).json()
        assert (worker_row["status"], worker_row["enabled"], worker_row["occupied_tasks"]) == (
            "OFFLINE",
            False,
            expected,
        )
        rows = (await client.get("/api/workers", params={"status": "OFFLINE", "limit": 200})).json()
        assert next(row for row in rows if row["id"] == worker_id)["occupied_tasks"] == expected
        assert (await client.delete(root)).status_code == 409
        config = {key: worker_row[key] for key in WorkerWrite.model_fields if key in worker_row}
        config["enabled"] = True
        edited = await client.put(root, json=config)
        assert edited.status_code == 200
        assert edited.json()["enabled"] and edited.json()["status"] == "OFFLINE"
        assert (await client.put(root, json={**config, "branch": "blocked"})).status_code == 409
        assert (await client.post(f"{root}/pause")).json()["status"] == "PAUSED"
        health = (await client.get(f"{root}/health")).json()
        assert health["connection"]["state"] == "offline"
        assert any(item["code"] == "occupied_task" for item in health["blocking"])
        assert not (await client.get(root)).json()["online"]
        online_before = (await client.get("/api/metrics")).json()["online_employees"]
        connected.add(worker_id)
        paused = (await client.get(root)).json()
        assert paused["status"] == "PAUSED" and paused["online"]
        assert (await client.get("/api/metrics")).json()["online_employees"] == online_before + 1
        task_connection = (await client.get(f"/api/tasks/{expected[0]['id']}")).json()
        assert task_connection["worker_online"] and task_connection["status"] == "RUNNING"
        resumed = (await client.post(f"{root}/resume")).json()
        assert resumed["status"] == "WORKING" and resumed["enabled"]
        assert resumed["occupied_tasks"] == expected
        assert (await client.get(f"{root}/health")).json()["connection"]["state"] == "online"
        for occupied in expected:
            result = await client.post(f"/api/tasks/{occupied['id']}/stop")
            assert result.json()["status"] == "CANCELED"
        assert (await client.get(root)).json()["occupied_tasks"] == []
        assert (await client.delete(root)).status_code == 200
        assert (await client.get(root)).status_code == 404


async def test_restart_preserves_active_session_and_allows_canceled_task(setup_runtime):
    import httpx

    from app.main import app
    from app.task_tools import TaskTools

    _, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await create_task(db, setup_runtime)
        task = await runtime.start(task.id)
        task_id, original = task.id, task.current_session_id
        assert (await TaskTools(db).intervene(task_id, "请补充验收条件"))["ok"]
        assert (await runtime.refresh(task_id)).waiting_reason == "intervention"
        with pytest.raises(RuntimeStateError, match="等待人工确认"):
            await runtime.confirm_completion(task_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        root = f"/api/tasks/{task_id}"
        assert (await client.post(f"{root}/restart")).status_code == 409
        async with SessionLocal() as db:
            assert not (await TaskTools(db).retry(task_id))["ok"]
        assert (await client.get(root)).json()["current_session_id"] == original
        assert (await client.post(f"{root}/stop")).json()["status"] == "CANCELED"
        async with SessionLocal() as db:
            assert (await TaskTools(db).retry(task_id))["status"] == "RUNNING"
        assert (await client.get(root)).json()["current_session_id"] != original

        assert (await client.post(f"{root}/stop")).json()["status"] == "CANCELED"
        restarted = (await client.post(f"{root}/restart")).json()
        assert restarted["status"] == "RUNNING" and restarted["current_session_id"] != original

        del native.sessions[restarted["current_session_id"]]
        async with SessionLocal() as db:
            assert (await RuntimeService(db).refresh(task_id)).waiting_reason == "session_missing"
            assert (await TaskTools(db).retry(task_id))["status"] == "RUNNING"
        assert (await client.get(root)).json()["current_session_id"] != restarted["current_session_id"]


async def test_completed_task_restart_is_rejected_by_rest_and_mcp(setup_runtime, monkeypatch):
    import httpx

    from app.main import app
    from app.task_mcp import build_mcp_server

    worker_id, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await create_task(db, setup_runtime)
        task = await runtime.start(task.id)
        task_id, sid = task.id, task.current_session_id
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task_id)
        assert (await runtime.confirm_completion(task_id)).status == "SUCCEEDED"
        with pytest.raises(RuntimeStateError, match="已完成的任务不能重新开始"):
            await runtime.start(task_id, restart=True)

    real_client = httpx.AsyncClient
    monkeypatch.setenv("NEXUS_INTERNAL_KEY", "runtime-mcp-test")
    monkeypatch.setattr("app.task_mcp.httpx.AsyncClient", lambda **kwargs: real_client(
        **{**kwargs, "transport": httpx.ASGITransport(app=app)},
    ))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        root = f"/api/tasks/{task_id}"
        original = (await client.get(root)).json()
        rejected = await client.post(f"{root}/restart")
        assert rejected.status_code == 409
        result = await build_mcp_server().call_tool("nexus_task_retry", {"task_id": task_id})
        assert not result.structured_content["ok"]
        assert result.structured_content["status"] == "SUCCEEDED"
        assert result.structured_content["error"] == rejected.json()["detail"]
        assert (await client.get(root)).json() == original
        worker = (await client.get(f"/api/workers/{worker_id}")).json()
        assert worker["status"] == "IDLE" and worker["occupied_tasks"] == []
        assert native.creates == 1 and native.aborts == 0


async def test_task_criteria_ids_survive_edits_and_reordering(setup_runtime):
    import httpx

    from app.main import app

    worker_id, category, _ = setup_runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            "/api/tasks",
            json={
                "task_type": "ISSUE",
                "category_id": category,
                "title": "验收项回归",
                "description": "测试验收项身份",
                "agent_worker_id": worker_id,
                "acceptance_criteria": [{"content": "甲"}, {"content": "乙"}],
            },
        )
        assert created.status_code == 201
        before = created.json()
        a, b = before["acceptance_criteria"]
        path = f"/api/tasks/{before['id']}"
        edited = await client.patch(
            path,
            json={
                "acceptance_criteria": [
                    {"id": b["id"], "content": "乙修订"},
                    {"content": "新增"},
                    a,
                ]
            },
        )
        assert edited.status_code == 200
        rows = (await client.get(path)).json()["acceptance_criteria"]
        assert rows[0] == {"id": b["id"], "content": "乙修订"} and rows[2] == a
        assert len({item["id"] for item in rows if item["id"]}) == 3


async def test_active_review_protects_source_worker_only_until_finished(setup_runtime, monkeypatch):
    import httpx

    from app.main import app
    from app.schemas import WorkerWrite

    worker_id, _, native = setup_runtime
    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        await runtime.start(task.id)
        native.sessions[task.current_session_id]["status"] = "idle"
        await runtime.refresh(task.id)
        await runtime.confirm_completion(task.id)
        job = await db.scalar(select(ExperienceReviewJob).where(ExperienceReviewJob.task_id == task.id))
        job_id = job.id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/workers/{worker_id}"
        worker = (await client.get(path)).json()
        assert worker["status"] == "IDLE" and not worker["occupied_tasks"]
        config = {key: worker[key] for key in WorkerWrite.model_fields if key in worker}
        renamed = await client.put(path, json={**config, "name": "新名称", "enabled": False})
        assert renamed.status_code == 200
        for result in (await client.delete(path), await client.put(path, json={**config, "branch": "next"})):
            assert result.status_code == 409 and "未完成经验复盘" in result.json()["detail"]
        async with SessionLocal() as db:
            job = await db.get(ExperienceReviewJob, job_id)
            job.status = "SUCCEEDED"
            await db.commit()
        assert (await client.put(path, json={**config, "branch": "next"})).status_code == 200
        assert (await client.delete(path)).status_code == 200


@pytest.mark.parametrize("action", ["deploy", "update", "restart"])
async def test_active_review_blocks_disruptive_maintenance(setup_runtime, monkeypatch, action):
    import httpx

    from app.main import app

    worker_id, _, native = setup_runtime
    monkeypatch.setattr(get_settings(), "experience_review_enabled", True)
    monkeypatch.setattr(get_settings(), "mcp_transport", "streamable-http")
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        worker.repository_url = "https://example.test/repository.git"
        worker.ssh_password = "fixture-password"
        await db.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Owner-Id": "review-test"},
    ) as client:
        path = f"/api/workers/{worker_id}/maintenance"
        previous = await client.post(path, json={"action": action})
        assert previous.status_code == 202
        operation_path = f"/api/maintenance/{previous.json()['id']}"
        assert (await client.post(operation_path + "/cancel")).status_code == 200

        async with SessionLocal() as db:
            task = await create_task(db, setup_runtime)
            runtime = RuntimeService(db)
            await runtime.start(task.id)
            native.sessions[task.current_session_id]["status"] = "idle"
            await runtime.refresh(task.id)
            await runtime.confirm_completion(task.id)
            assert task.status == "SUCCEEDED"
            job = await db.scalar(select(ExperienceReviewJob).where(ExperienceReviewJob.task_id == task.id))
            job_id = job.id

        for review_status in ("PENDING", "ANALYZING", "RETRY_WAIT"):
            async with SessionLocal() as db:
                job = await db.get(ExperienceReviewJob, job_id)
                job.status = review_status
                await db.commit()
            for result in (
                await client.post(path, json={"action": action}),
                await client.post(operation_path + "/retry"),
            ):
                assert result.status_code == 409
                assert "未完成经验复盘" in result.json()["detail"]

        check = await client.post(path, json={"action": "check"})
        assert check.status_code == 202
        assert (await client.post(f"/api/maintenance/{check.json()['id']}/cancel")).status_code == 200

        async with SessionLocal() as db:
            job = await db.get(ExperienceReviewJob, job_id)
            job.status = "SUCCEEDED"
            await db.commit()
        retried = await client.post(operation_path + "/retry")
        assert retried.status_code == 200 and retried.json()["attempt"] == 2
        assert (await client.post(operation_path + "/cancel")).status_code == 200
        assert (await client.post(path, json={"action": action})).status_code == 202


async def test_event_and_maintenance_history_use_bounded_id_windows(setup_runtime):
    import httpx

    from app.main import app
    from app.models import Event, WorkerMaintenanceEvent

    worker_id, _, _ = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task_id = task.id
        operation = WorkerMaintenanceOperation(
            id=f"{worker_id}-log",
            agent_worker_id=worker_id,
            action="deploy",
            state="SUCCEEDED",
        )
        db.add(operation)
        await db.flush()
        db.add_all(Event(task_id=task_id, event_type="TEST", message=str(i)) for i in range(1200))
        db.add_all(
            WorkerMaintenanceEvent(
                operation_id=operation.id,
                attempt=1,
                type="step",
                message=str(i),
            )
            for i in range(1200)
        )
        await db.commit()
        operation_id = operation.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/tasks/{task_id}/events"
        latest = (await client.get(path)).json()
        assert len(latest) == 100 and latest[0]["message"] == "1199"
        previous = (await client.get(path, params={"before_id": latest[-1]["id"]})).json()
        assert len(previous) == 100 and previous[0]["message"] == "1099"
        async with SessionLocal() as db:
            db.add(Event(task_id=task_id, event_type="TEST", message="increment"))
            await db.commit()
        delta = (await client.get(path, params={"after_id": latest[0]["id"]})).json()
        assert [row["message"] for row in delta] == ["increment"]
        op_path = f"/api/maintenance/{operation_id}"
        tail = (await client.get(op_path)).json()["events"]
        assert len(tail) == 100 and tail[0]["message"] == "1100"
        history = (await client.get(f"{op_path}/events/page", params={"before_id": tail[0]["id"]})).json()
        assert len(history["items"]) == 200
        assert history["items"][-1]["message"] == "1099"
        assert history["next_cursor"] == history["items"][0]["id"]


async def test_mysql_cancel_replies_while_workspace_preparation_holds_lock(monkeypatch):
    import os

    import httpx
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.database import get_session
    from app.main import app
    from app.runtime import lifecycle

    url = os.environ.get("ASTRA_TEST_MYSQL_URL")
    if not url:
        pytest.skip("需要 ASTRA_TEST_MYSQL_URL 指向已初始化的隔离 MySQL")
    engine = create_async_engine(make_url(url).set(drivername="mysql+asyncmy"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    worker_id, task_id = f"lock-worker-{uuid4().hex}", f"lock-task-{uuid4().hex}"
    preparing, release = asyncio.Event(), asyncio.Event()

    async def get_test_session():
        async with factory() as db:
            yield db

    async def slow_prepare(*_args):
        preparing.set()
        await release.wait()
        raise RuntimeError("准备失败")

    async def start():
        async with factory() as db:
            with pytest.raises(RuntimeError, match="准备失败"):
                await RuntimeService(db).start(task_id)

    monkeypatch.setitem(app.dependency_overrides, get_session, get_test_session)
    monkeypatch.setattr(lifecycle, "prepare_workspace", slow_prepare)
    async with factory() as db:
        db.add(AgentWorker(id=worker_id, name=worker_id, status="IDLE", mode="LINUX_REMOTE"))
        await db.flush()
        db.add(
            Task(
                id=task_id,
                title=task_id,
                description="准备延迟",
                task_type="ISSUE",
                agent_worker_id=worker_id,
            )
        )
        await db.commit()

    try:
        running = asyncio.create_task(start())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            path = f"/api/tasks/{task_id}/stop"
            try:
                await asyncio.wait_for(preparing.wait(), 3)
                refused = await asyncio.wait_for(client.post(path), 1)
                assert refused.status_code == 409 and "重试停止" in refused.json()["detail"]
                async with factory() as db:
                    assert (await db.get(Task, task_id)).status == "RUNNING"
                    assert (await db.get(AgentWorker, worker_id)).status == "WORKING"
            finally:
                release.set()
                await running
            assert (await client.post(path)).json()["status"] == "CANCELED"
            async with factory() as db:
                assert (await db.get(AgentWorker, worker_id)).status == "IDLE"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("busy", [False, True])
async def test_confirmed_native_current_round_failure_requires_idle(setup_runtime, busy):
    worker_id, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await runtime.start((await create_task(db, setup_runtime)).id)
        sid = task.current_session_id
        native.sessions[sid]["messages"].append({"info": {
            "id": "msg_assistant", "role": "assistant", "parentID": task.last_message_id,
            "error": {"name": "APIError", "data": {"message": "execution failed"}},
        }})
        native.sessions[sid]["status"] = "busy" if busy else "idle"
        await runtime.refresh(task.id)
        assert task.status == ("RUNNING" if busy else "FAILED")
        assert (await db.get(AgentWorker, worker_id)).status == ("WORKING" if busy else "IDLE")
        if not busy:
            assert task.finished_at
            assert await db.scalar(select(Task.id).where(occupying_clause(worker_id))) is None
            task = await runtime.restart(task.id)
            assert task.status == "RUNNING" and task.current_session_id != sid


async def test_prior_round_failure_does_not_fail_current_task(setup_runtime):
    _, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await runtime.start((await create_task(db, setup_runtime)).id)
        native.sessions[task.current_session_id]["status"] = "idle"
        native.sessions[task.current_session_id]["messages"].append({"info": {
            "id": "msg_old", "role": "assistant", "parentID": "prior_input",
            "error": {"name": "APIError"},
        }})
        await runtime.refresh(task.id)
        assert task.status == "WAITING" and task.waiting_reason == "confirmation"


async def test_periodic_reconciliation_retries_after_failure_without_any_event(setup_runtime, monkeypatch):
    from app.runtime import recovery

    worker, _, native = setup_runtime
    reads, subscribed = [], asyncio.Event()
    original_status = native.status

    async def status(directory, sid):
        reads.append(sid)
        if len(reads) <= 2:
            raise ConnectionError("temporary SSH disconnect")
        return await original_status(directory, sid)

    async def events(directory, *, on_subscribed=None):
        if on_subscribed:
            on_subscribed()
        subscribed.set()
        await asyncio.Event().wait()
        yield {}  # An open SSE stream never emits a state event.

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    async with SessionLocal() as db:
        task = await RuntimeService(db).start((await create_task(db, setup_runtime)).id)
        task_id, sid = task.id, task.current_session_id
    native.status, native.events = status, events
    monkeypatch.setattr(recovery, "connect", connect)
    monkeypatch.setattr(recovery, "_SCAN_SECONDS", 0.01)
    watcher = asyncio.create_task(recovery.watch_session(task_id))
    try:
        await asyncio.wait_for(subscribed.wait(), 2)
        for _ in range(100):
            async with SessionLocal() as db:
                current = await db.get(Task, task_id)
                assert current.agent_worker_id == worker and current.current_session_id == sid
                assert await db.scalar(select(Task.id).where(occupying_clause(worker))) == task_id
                if len(reads) > 2:
                    assert current.status == "RUNNING"
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("对账失败没有自动重试")
        native.sessions[sid]["status"] = "idle"
        for _ in range(100):
            async with SessionLocal() as db:
                current = await db.get(Task, task_id)
                if current.status == "WAITING" and current.waiting_reason == "confirmation":
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("没有 SSE 通知时未进行周期对账")
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize("kind", ["question", "permission"])
async def test_human_reply_uses_native_client_then_reconciles(setup_runtime, monkeypatch, kind):
    import json

    import httpx

    from app.executors.opencode_server import OpenCodeServerClient
    from app.runtime import assistance, lifecycle
    from app.runtime.assistance import WorkerAssistance

    worker_id, _, _ = setup_runtime
    pending, writes = True, []

    def respond(request):
        nonlocal pending
        path = request.url.path
        assert request.url.params["directory"] == "/workspace/human-reply"
        if request.method == "POST":
            assert path == f"/{kind}/req_current/reply"
            writes.append(json.loads(request.content))
            pending = False
            return httpx.Response(200, json=True)
        if path.endswith("/children"):
            return httpx.Response(200, json=[])
        if path == "/session/ses_hitl":
            return httpx.Response(200, json={"id": "ses_hitl"})
        if path == "/session/status":
            return httpx.Response(200, json={"ses_hitl": {"type": "busy" if not pending else "idle"}})
        if path in {"/question", "/permission"}:
            items = [{"id": "req_current", "sessionID": "ses_hitl", "questions": [{"question": "确认?"}]}]
            return httpx.Response(200, json=items if pending and path == f"/{kind}" else [])
        raise AssertionError(path)

    client = OpenCodeServerClient(base_url="http://native.invalid")
    real_http_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_http_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield client

    monkeypatch.setattr(assistance, "connect", connect)
    monkeypatch.setattr(lifecycle, "connect", connect)
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        task.status, task.waiting_reason = "WAITING", kind
        task.current_session_id, task.last_message_id = "ses_hitl", "msg_current"
        task.workspace = "/workspace/human-reply"
        (await db.get(AgentWorker, worker_id)).status = "WORKING"
        await db.commit()
        response = await WorkerAssistance(RuntimeService(db)).reply(
            task.id, expected_session_id="ses_hitl", request_id="req_current", kind=kind,
            **({"answers": [["确认"]]} if kind == "question" else {"decision": "once"}),
        )
        assert response["accepted"] and not response["refresh_required"]
        assert task.status == "RUNNING" and task.waiting_reason is None
        assert writes == ([{"answers": [["确认"]]}] if kind == "question" else [{"reply": "once"}])


@pytest.mark.parametrize("failed", [False, True])
async def test_idle_with_unreadable_result_waits_and_reconciles(setup_runtime, monkeypatch, failed):
    worker_id, _, native = setup_runtime
    async with SessionLocal() as db:
        runtime = RuntimeService(db)
        task = await runtime.start((await create_task(db, setup_runtime)).id)
        sid = task.current_session_id
        native.sessions[sid]["status"] = "idle"
        if failed:
            native.sessions[sid]["messages"].append({"info": {
                "id": "msg_error", "role": "assistant", "parentID": task.last_message_id,
                "error": {"name": "APIError"},
            }})
        original = native.messages_page

        async def unreadable(*args, **kwargs):
            raise TimeoutError("temporary result read failure")

        monkeypatch.setattr(native, "messages_page", unreadable)
        await runtime.refresh(task.id)
        assert task.status == "WAITING" and task.waiting_reason == "result_pending"
        assert await db.scalar(select(Task.id).where(occupying_clause(worker_id))) == task.id
        assert (await db.get(AgentWorker, worker_id)).status == "WORKING"
        with pytest.raises(RuntimeStateError, match="等待人工确认"):
            await runtime.confirm_completion(task.id)
        await db.commit()
        monkeypatch.setattr(native, "messages_page", original)
        await runtime.refresh(task.id)
        assert task.current_session_id == sid and native.aborts == 0
        assert task.status == ("FAILED" if failed else "WAITING")
        if not failed:
            assert task.waiting_reason == "confirmation"
            await runtime.confirm_completion(task.id)
            assert task.status == "SUCCEEDED"
        assert (await db.get(AgentWorker, worker_id)).status == "IDLE"
