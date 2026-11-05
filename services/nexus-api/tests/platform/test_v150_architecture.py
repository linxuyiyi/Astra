"""v1.5.0 最终结构、Task 独占、原生执行、认证代次、重发和复盘回归。"""

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
    WorkerCommand,
    WorkerHttpSlot,
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
from app.schemas import TaskCreate, WorkerRead
from app.services import TaskService
from app.worker_gateway.bus import MemoryRouteBus
from app.worker_gateway.data_channel import DataChannel, retire_submission
from app.worker_gateway.protocol import WorkerGatewayError, envelope
from app.worker_gateway.service import WorkerGateway


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
    assert len(tables["t_nexus_agent_workers"].columns) == 26
    assert len(tables["t_nexus_worker_commands"].columns) == 20
    assert len(tables["t_nexus_worker_maintenance_operations"].columns) == 24
    assert not {
        "t_nexus_runtime_sessions",
        "t_nexus_worker_instances",
        "t_nexus_opencode_execution_environments",
        "t_nexus_worker_connections",
    } & set(tables)
    assert {s.value for s in TaskStatus} == {"PENDING", "RUNNING", "WAITING", "SUCCEEDED", "CANCELED"}
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


async def test_missing_native_session_recreates_only_session(setup_runtime):
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


async def test_gateway_auth_epoch_and_old_disconnect(setup_runtime):
    worker, _, _ = setup_runtime
    gateway = WorkerGateway(bus=MemoryRouteBus())
    async with SessionLocal() as db:
        enrolled = await gateway.enroll(db, worker)
        message = envelope(
            request_id="auth",
            agent_worker_id=worker,
            session_id=None,
            message_type="command",
            payload={"name": "authenticate", "agent_version": "1.5.0"},
        )
        first = await gateway.authenticate(db, message, enrolled.token)
        second = await gateway.authenticate(db, message, enrolled.token)
        assert second.epoch > first.epoch
        await gateway.disconnect(db, worker, first.epoch)
        await db.refresh(await db.get(AgentWorker, worker))
        assert (await db.get(AgentWorker, worker)).status == "IDLE"
        with pytest.raises(WorkerGatewayError):
            await gateway.authenticate(db, message, "wrong-token")


async def test_worker_secret_fields_are_not_serialized(setup_runtime):
    worker, _, _ = setup_runtime
    async with SessionLocal() as db:
        row = await db.get(AgentWorker, worker)
        row.ssh_password = "encrypted"
        row.git_token_ciphertext = "encrypted"
        row.token_digest = "digest"
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


async def test_retire_command_releases_http_slot(setup_runtime):
    worker, _, _ = setup_runtime
    request_id = "http_" + uuid4().hex
    async with SessionLocal() as db:
        db.add(
            WorkerCommand(
                request_id=request_id,
                agent_worker_id=worker,
                name="http",
                epoch=1,
                status="accepted",
                slot_held=True,
            )
        )
        db.add(WorkerHttpSlot(agent_worker_id=worker, epoch=1, inflight=1))
        await db.commit()
        await retire_submission(request_id)
        await retire_submission(request_id)
        slot = await db.get(WorkerHttpSlot, (worker, 1), populate_existing=True)
        assert slot.inflight == 0


async def test_gateway_http_roundtrip_and_cross_worker_isolation(setup_runtime):
    worker, _, _ = setup_runtime
    bus = MemoryRouteBus()
    gateway = WorkerGateway(bus=bus)
    request_id = "http_" + uuid4().hex
    async with SessionLocal() as db:
        enrolled = await gateway.enroll(db, worker)
        auth = await gateway.authenticate(
            db,
            envelope(
                request_id="auth",
                agent_worker_id=worker,
                session_id=None,
                message_type="command",
                payload={"name": "authenticate"},
            ),
            enrolled.token,
        )
        await bus.set_owner(worker, gateway.owner_id, auth.epoch)
        await bus.bind(worker, epoch=auth.epoch, owner_id=gateway.owner_id)
        result = await gateway.dispatch(
            db,
            agent_worker_id=worker,
            request_id=request_id,
            name="http",
            payload={"method": "GET", "path": "/global/health", "body": {}},
        )
        assert result.delivered
        channel = DataChannel(gateway)
        fetched = await channel.fetch_request(
            db, request_id=request_id, agent_worker_id=worker, epoch=auth.epoch
        )
        assert fetched["path"] == "/global/health"
        with pytest.raises(WorkerGatewayError):
            await channel.fetch_request(
                db, request_id=request_id, agent_worker_id="other-worker", epoch=auth.epoch
            )
        await channel.commit_upload(
            db,
            request_id=request_id,
            agent_worker_id=worker,
            epoch=auth.epoch,
            upload_id="body",
            http_status=200,
            headers={},
            content_type="application/json",
            chunk='{"healthy":true}',
            complete=True,
        )
        response = await bus.wait_response(request_id, 1)
        assert response["body"] == '{"healthy":true}'
        assert (
            await db.scalar(select(WorkerCommand).where(WorkerCommand.request_id == request_id))
        ).status == "succeeded"


async def test_old_epoch_http_body_is_rejected_after_reconnect(setup_runtime):
    worker, _, _ = setup_runtime
    bus = MemoryRouteBus()
    gateway = WorkerGateway(bus=bus)
    async with SessionLocal() as db:
        enrolled = await gateway.enroll(db, worker)
        message = envelope(
            request_id="auth",
            agent_worker_id=worker,
            session_id=None,
            message_type="command",
            payload={"name": "authenticate"},
        )
        first = await gateway.authenticate(db, message, enrolled.token)
        await bus.set_owner(worker, gateway.owner_id, first.epoch)
        await bus.bind(worker, epoch=first.epoch, owner_id=gateway.owner_id)
        result = await gateway.dispatch(
            db,
            agent_worker_id=worker,
            request_id="http_" + uuid4().hex,
            name="http",
            payload={"method": "GET", "path": "/global/health", "body": {}},
        )
        second = await gateway.authenticate(db, message, enrolled.token)
        assert second.epoch > first.epoch
        with pytest.raises(WorkerGatewayError):
            await DataChannel(gateway).fetch_request(
                db, request_id=result.command.request_id, agent_worker_id=worker, epoch=first.epoch
            )


async def test_continue_reuses_session_without_sending_another_prompt(setup_runtime):
    _, _, native = setup_runtime
    async with SessionLocal() as db:
        task = await create_task(db, setup_runtime)
        runtime = RuntimeService(db)
        task = await runtime.start(task.id)
        sid = task.current_session_id
        native.sessions[sid]["status"] = "idle"
        await runtime.refresh(task.id)
        await runtime.continue_task(task.id)
        assert task.current_session_id == sid and len(native.sent) == 1 and task.status == "WAITING"


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

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    monkeypatch.setattr(trace_reader, "connect", connect)
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


async def test_paused_worker_remains_paused_across_disconnect_and_reconnect(setup_runtime):
    worker, _, _ = setup_runtime
    gateway = WorkerGateway(bus=MemoryRouteBus())
    async with SessionLocal() as db:
        row = await db.get(AgentWorker, worker)
        row.status = "PAUSED"
        await db.commit()
        enrolled = await gateway.enroll(db, worker)
        auth = envelope(
            request_id="paused-auth",
            agent_worker_id=worker,
            session_id=None,
            message_type="command",
            payload={"name": "authenticate"},
        )
        first = await gateway.authenticate(db, auth, enrolled.token)
        await gateway.disconnect(db, worker, first.epoch)
        await db.refresh(row)
        assert row.status == "PAUSED"
        await gateway.authenticate(db, auth, enrolled.token)
        await db.refresh(row)
        assert row.status == "PAUSED"


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


async def test_paused_worker_still_accepts_native_control_calls(setup_runtime):
    from app.runtime.worker_transport import WorkerCommandTransport

    worker, _, _ = setup_runtime
    async with SessionLocal() as db:
        row = await db.get(AgentWorker, worker)
        row.status = "PAUSED"
        await db.commit()
        assert (await WorkerCommandTransport(agent_worker_id=worker)._instance(db)).id == worker


async def test_unknown_mutating_maintenance_keeps_worker_locked(setup_runtime, monkeypatch):
    from app.runtime import maintenance
    from app.runtime.worker_client import WorkerOpenCodeClient

    worker, _, _ = setup_runtime

    async def unknown(*args, **kwargs):
        raise TimeoutError("remote update acceptance unknown")

    monkeypatch.setattr(WorkerOpenCodeClient, "run_command", unknown)
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
        with pytest.raises(HTTPException):
            await submit_operation(db, worker, "check")


async def test_health_probe_reads_the_current_worker_without_instance_aliases(setup_runtime, monkeypatch):
    from app.runtime.worker_client import WorkerOpenCodeClient
    from app.runtime.worker_health import probe_online_worker

    worker_id, _, _ = setup_runtime

    async def inspect(client, name, body):
        assert client.agent_worker_id == worker_id and name == "inspect_runtime"
        assert body["expected_opencode_version"] == "1.18.35"
        return {"opencode": {"status": "healthy"}, "spec_revision": "verified-source"}

    monkeypatch.setattr(WorkerOpenCodeClient, "run_command", inspect)
    async with SessionLocal() as db:
        worker = await db.get(AgentWorker, worker_id)
        worker.agent_version = "1.5.0"
        await db.commit()
        result = await probe_online_worker(worker, {"opencode": {"version": "1.18.35"}})
        assert result["spec_revision"] == "verified-source"
        assert result["worker_agent_version"] == "1.5.0"
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
        binding = (await client.get(f"/api/tasks/{task_id}/workspace")).json()
        assert binding["runtime"]["workspace_ui"] is None
        assert "Worker 不在线" in binding["runtime"]["workspace_ui_error"]
