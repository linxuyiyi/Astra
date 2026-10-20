"""经验复盘：幂等、租约、切片、证据校验与状态机。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import (
    AgentWorker,
    Event,
    ExperienceEvidence,
    ExperienceReviewJob,
    KnowledgeItem,
    RuntimeSession,
    Task,
)
from app.runtime.experience_review import coordinator
from app.runtime.experience_review.evidence import EvidenceError, validate_candidate
from app.runtime.experience_review.schemas import Candidate, TraceEvent, TraceResult
from app.runtime.experience_review.trace_segmenter import segment_events


def _trace() -> TraceResult:
    events = [
        TraceEvent(
            ref="trace-000001",
            session_id="ses_1",
            message_id="msg_1",
            part_id="prt_1",
            role="user",
            kind="user_input",
            excerpt="修复参数升级",
            content_digest="a",
        ),
        TraceEvent(
            ref="trace-000002",
            session_id="ses_1",
            message_id="msg_2",
            part_id="prt_2",
            role="assistant",
            kind="tool_result",
            tool="bash",
            status="failed",
            exit_code=1,
            excerpt="AssertionError",
            content_digest="b",
        ),
        TraceEvent(
            ref="trace-000003",
            session_id="ses_1",
            message_id="msg_3",
            part_id="prt_3",
            role="assistant",
            kind="verification",
            status="passed",
            excerpt="tests passed",
            content_digest="c",
        ),
    ]
    return TraceResult(
        events=events,
        sessions=["ses_1"],
        source_digest="digest",
        message_count=3,
    )


def _candidate(refs: list[str], *, kind: str = "domain") -> Candidate:
    return Candidate(
        kind=kind,
        title="升级脚本先校验规则",
        summary="先读取规则再生成",
        content_md="生成参数升级脚本前先读取规则并验证。",
        evidence_refs=refs,
        target_component="tool_guidance" if kind == "harness_improvement" else None,
    )


def test_segment_keeps_causal_events_and_refs():
    segments = segment_events(_trace().events, budget_tokens=1)
    assert [event.ref for segment in segments for event in segment.events] == [
        "trace-000001",
        "trace-000002",
        "trace-000003",
    ]
    assert all(segment.digest for segment in segments)


def test_large_trace_is_split_without_losing_refs():
    events = [
        TraceEvent(
            ref=f"trace-{index:06d}",
            session_id="ses_1",
            message_id=f"msg_{index}",
            part_id=f"prt_{index}",
            role="assistant",
            kind="tool_result",
            tool="bash",
            status="passed",
            excerpt="x" * 400,
            content_digest=str(index),
        )
        for index in range(1, 401)
    ]
    segments = segment_events(events, budget_tokens=200)
    assert len(segments) > 1
    flattened = [event.ref for segment in segments for event in segment.events]
    assert flattened == [event.ref for event in events]
    assert all(segment.token_estimate > 0 for segment in segments)


def test_evidence_rejects_missing_cross_session_and_secret():
    trace = _trace()
    with pytest.raises(EvidenceError) as missing:
        validate_candidate(_candidate([]), trace)
    assert missing.value.code == "missing_evidence"

    cross = _trace()
    cross.events[0].session_id = "ses_other"
    with pytest.raises(EvidenceError) as wrong:
        validate_candidate(_candidate(["trace-000001"]), cross)
    assert wrong.value.code == "cross_session_ref"

    secret = _candidate(["trace-000001"])
    secret.content_md = "api_key = sk-abcdefghijklmnopqrstuvwx"
    with pytest.raises(EvidenceError) as leaked:
        validate_candidate(secret, trace)
    assert leaked.value.code == "secret_detected"


def test_harness_candidate_requires_target():
    candidate = _candidate(["trace-000002"], kind="harness_improvement")
    candidate.target_component = None
    with pytest.raises(EvidenceError) as exc:
        validate_candidate(candidate, _trace())
    assert exc.value.code == "missing_harness_target"


async def _task_and_event() -> tuple[str, int]:
    from uuid import uuid4

    async with SessionLocal() as session:
        task = Task(id=f"T-{uuid4().hex[:8]}", title="review", task_type="ISSUE", status="WAITING")
        session.add(task)
        await session.flush()
        event = Event(task_id=task.id, event_type="TASK_COMPLETION_CONFIRMED", phase="review", message="ok")
        session.add(event)
        await session.commit()
        return task.id, event.id


@pytest.mark.asyncio
async def test_enqueue_is_idempotent(monkeypatch):
    task_id, event_id = await _task_and_event()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        event = await session.get(Event, event_id)
        snapshot = {
            "task_id": task_id,
            "root_session_id": "ses_1",
            "turn_anchor_id": "msg_1",
            "confirmed_at": datetime.now(UTC).isoformat(),
            "source_session_ids": ["ses_1"],
        }
        first = await coordinator.enqueue_in_transaction(session, task=task, event=event, snapshot=snapshot)
        second = await coordinator.enqueue_in_transaction(session, task=task, event=event, snapshot=snapshot)
        await session.commit()
        assert first.id == second.id
        count = await session.scalar(select(func.count()).select_from(ExperienceReviewJob))
        assert count == 1


@pytest.mark.asyncio
async def test_claim_lease_and_retry(monkeypatch):
    task_id, event_id = await _task_and_event()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        event = await session.get(Event, event_id)
        snapshot = {
            "task_id": task_id,
            "root_session_id": "ses_1",
            "confirmed_at": datetime.now(UTC).isoformat(),
            "source_session_ids": ["ses_1"],
        }
        job = await coordinator.enqueue_in_transaction(session, task=task, event=event, snapshot=snapshot)
        await session.commit()
        job_id = job.id

    async with SessionLocal() as session:
        claimed = await coordinator.claim_job(session, job_id, "worker-a")
        assert claimed is not None
        again = await coordinator.claim_job(session, job_id, "worker-b")
        assert again is None
        claimed.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    async with SessionLocal() as session:
        reclaimed = await coordinator.claim_job(session, job_id, "worker-b")
        assert reclaimed is not None
        reclaimed.status = coordinator.FAILED
        await session.commit()

    async with SessionLocal() as session:
        retried = await coordinator.retry_job(session, job_id)
        assert retried is not None
        assert retried.status == coordinator.PENDING


class FakeLLM:
    def __init__(self):
        self.calls = 0

    async def complete_json(self, system: str, user: str):
        self.calls += 1
        if "归纳器" in system:
            return {
                "candidates": [
                    {
                        "kind": "domain",
                        "title": "升级前先读规则",
                        "summary": "先读规则",
                        "content_md": "生成升级脚本前先读取规则并验证。",
                        "evidence_refs": ["trace-000001", "trace-000002"],
                    }
                ]
            }
        return {
            "observations": [
                {
                    "problem": "遗漏规则",
                    "attempt": "直接生成",
                    "outcome": "failed",
                    "correction": "先读规则",
                    "verification": "passed",
                    "evidence_refs": ["trace-000001", "trace-000002"],
                }
            ]
        }

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_process_job_map_reduce_and_write(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "experience_review_enabled", True)
    monkeypatch.setattr(settings, "experience_review_shadow_mode", False)
    task_id, event_id = await _task_and_event()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        event = await session.get(Event, event_id)
        snapshot = {
            "task_id": task_id,
            "root_session_id": "ses_1",
            "confirmed_at": datetime.now(UTC).isoformat(),
            "source_session_ids": ["ses_1"],
            "task": {"id": task_id, "title": "review"},
        }
        job = await coordinator.enqueue_in_transaction(session, task=task, event=event, snapshot=snapshot)
        await session.commit()
        job_id = job.id

    async def fake_read(_snapshot):
        return _trace()

    written: list[str] = []

    async def fake_write(_session, *, job, trace, candidates):
        written.extend(candidate.title for candidate in candidates)
        return ["KNOW-1"]

    monkeypatch.setattr(coordinator, "read_trace", fake_read)
    monkeypatch.setattr(coordinator, "write_candidates", fake_write)
    llm = FakeLLM()

    for _ in range(4):
        result = await coordinator.process_job(job_id, llm=llm)
        assert result is not None

    async with SessionLocal() as session:
        job = await session.get(ExperienceReviewJob, job_id)
        assert job is not None
        assert job.status == coordinator.SUCCEEDED
        assert job.candidate_ids == ["KNOW-1"]
    assert written == ["升级前先读规则"]


@pytest.mark.asyncio
async def test_candidate_writer_persists_validated_evidence():
    from uuid import uuid4

    from app.runtime.experience_review.candidate_writer import write_candidates

    suffix = uuid4().hex[:8]
    async with SessionLocal() as session:
        employee = AgentWorker(id=f"E-{suffix}", name="reviewer", role="worker")
        task = Task(id=f"T-{suffix}", title="review", task_type="ISSUE", employee_id=employee.id)
        binding = RuntimeSession(
            id=f"ses_{suffix}",
            task_id=task.id,
            employee_id=employee.id,
            workspace="/tmp/workspace",
            environment={},
            status="SUCCEEDED",
        )
        job = ExperienceReviewJob(
            id=f"xrj_{suffix}",
            confirmation_event_id=int(uuid4().int % 1_000_000_000),
            review_key="a" * 64,
            task_id=task.id,
            task_id_snapshot=task.id,
            root_session_id=binding.id,
            confirmed_at=datetime.now(UTC),
            source_snapshot={"root_session_id": binding.id},
            status="WRITING",
            phase="WRITING",
            next_retry_at=datetime.now(UTC),
            prompt_version="1",
            model_id="test",
            candidate_ids=[],
        )
        session.add_all([employee, task, binding, job])
        await session.commit()

    trace = _trace()
    candidate = _candidate(["trace-000001", "trace-000002"])
    async with SessionLocal() as session:
        job = await session.get(ExperienceReviewJob, f"xrj_{suffix}")
        ids = await write_candidates(session, job=job, trace=trace, candidates=[candidate])
        await session.commit()
        assert ids
        item = await session.get(KnowledgeItem, ids[0])
        assert item is not None
        assert item.status == "candidate"
        assert item.origin == "system_review"
        evidence = (
            await session.scalars(
                select(ExperienceEvidence).where(ExperienceEvidence.knowledge_id == item.id)
            )
        ).all()
        assert evidence
        assert all(row.validation_status == "validated" for row in evidence)
        assert evidence[0].native_session_id == "ses_1"


@pytest.mark.asyncio
async def test_review_jobs_api_lists_and_retries(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app as platform_app

    task_id, event_id = await _task_and_event()
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        event = await session.get(Event, event_id)
        snapshot = {
            "task_id": task_id,
            "root_session_id": "ses_1",
            "confirmed_at": datetime.now(UTC).isoformat(),
            "source_session_ids": ["ses_1"],
        }
        job = await coordinator.enqueue_in_transaction(session, task=task, event=event, snapshot=snapshot)
        await session.commit()
        job_id = job.id

    with TestClient(platform_app) as client:
        listed = client.get("/api/library/review-jobs", params={"task_id": task_id})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [job_id]
        detail = client.get(f"/api/library/review-jobs/{job_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "PENDING"

    async with SessionLocal() as session:
        job = await session.get(ExperienceReviewJob, job_id)
        job.status = coordinator.FAILED
        await session.commit()

    with TestClient(platform_app) as client:
        retried = client.post(f"/api/library/review-jobs/{job_id}/retry")
        assert retried.status_code == 200
        assert retried.json()["status"] == "PENDING"


def test_worker_trace_read_trimming_and_cursor(tmp_path, monkeypatch):
    import json

    from app.worker_agent import trace_read

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    payload = [
        {
            "info": {"id": "msg_1", "role": "assistant"},
            "parts": [
                {
                    "id": "prt_1",
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "passed", "output": "x" * 20000},
                }
            ],
        }
    ]

    class Response:
        headers = {"x-next-cursor": "msg_0"}

        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(trace_read.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    result = trace_read.read_trace_from_payload(
        "http://127.0.0.1:4096",
        str(tmp_path),
        {"session_id": "ses_1", "workspace": str(workspace), "limit": 20},
    )
    assert result["next_before"] == "msg_0"
    assert result["messages"][0]["parts"][0]["state"]["output"].endswith("x")
    assert len(result["messages"][0]["parts"][0]["state"]["output"]) < 20000
