"""Assistant review acceptance: source facts, atomic candidates and retry isolation."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.assistant import review
from app.domain import ExecutionMode
from app.models import (
    AgentWorker,
    Base,
    KnowledgeItem,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
    TaskReview,
    TaskSummary,
)
from app.runtime.environment import environment_snapshot
from app.runtime.lifecycle import RuntimeService, RuntimeStateError

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SKILL_PATH = (
    REPOSITORY_ROOT / ".opencode" / "skills"
    / "nexus-task-experience-review" / "SKILL.md"
)


@pytest.fixture
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="员工", role="build"))
        environment = OpenCodeExecutionEnvironment(
            employee_id="E1", mode="LOCAL", name="test", workspace_root="/bound", status="AVAILABLE",
        )
        session.add(environment)
        await session.flush()
        session.add(
            Task(
                id="T1",
                title="测试任务",
                task_type="ISSUE",
                employee_id="E1",
                status="SUCCEEDED",
                current_session_id="ses_1",
                last_message_id="msg_001",
            )
        )
        session.add(
            RuntimeSession(
                id="ses_1",
                task_id="T1",
                employee_id="E1",
                workspace="/bound",
                environment=environment_snapshot(environment),
                status="SUCCEEDED",
                last_message_id="msg_200",
            )
        )
        await session.commit()
    monkeypatch.setattr(review, "SessionLocal", factory)
    native = [
        {"info": {"id": "msg_002", "role": "user"},
         "parts": [{"id": "prt_2", "type": "text", "text": "人工纠正"}]},
        {"info": {"id": "msg_test", "role": "assistant"}, "parts": [
            {"id": "prt_test", "type": "tool", "tool": "bash",
             "state": {"status": "completed", "input": {"command": "pytest"},
                       "output": "2 passed"}},
        ]},
    ]

    @asynccontextmanager
    async def native_connection(*args, **kwargs):
        yield SimpleNamespace(
            children=AsyncMock(return_value=[]),
            messages_page=AsyncMock(return_value={"messages": native, "next_before": None}),
        )

    monkeypatch.setattr(review, "connect", native_connection)
    yield factory
    await engine.dispose()


async def submit(session, text="任务总结，无可复用经验", experiences=None, source="ses_1", token=None):
    task = await session.get(Task, "T1")
    binding = await session.get(RuntimeSession, source)
    return await review.submit_review(
        session,
        task_id="T1",
        source_session_id=source,
        expected_review_id=token or review.review_id(task, binding),
        task_summary=text,
        experiences=experiences or [],
    )


@pytest.mark.parametrize(
    "task_type,summary,rule",
    [
        (
            "REQUIREMENT",
            "需求与关键澄清：AI 误以为删除数据；人工纠正为仅隐藏。\n最终方案：展示层隐藏。\n"
            "实现与验证：确认数据库保留数据。\n注意事项：人工纠正 msg_002。\n"
            "可复用经验：先澄清数据保留语义。",
            "遇到隐藏需求，先确认是否保留数据，再调整展示层，验证隐藏后数据仍可恢复。",
        ),
        (
            "ISSUE",
            "问题与关键现象：缓存未更新。\n排查与根因：AI 误判网络；人工补充 msg_002：直连同样失败，"
            "最终定位缓存失效遗漏。\n修复与验证：补充失效，回归通过。\n注意事项：排除网络不能仅靠猜测。\n"
            "可复用经验：对照直连与缓存路径。",
            "遇到更新不可见，比较直连和缓存路径，定位失效环节，修复后验证更新前后读取一致。",
        ),
    ],
)
async def test_structured_review_preserves_correction_and_candidate(factory, task_type, summary, rule):
    # These are model-authored fixtures; this tests transport/persistence, not LLM quality.
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.task_type = task_type
        await session.commit()
        experience = review.ReviewExperience(
            title="判断和验证规则",
            content_md=rule,
            evidence=[
                {
                    "message_id": "msg_002",
                    "part_id": "prt_2",
                    "evidence_type": "human_correction",
                    "content": "人工纠正",
                }
            ],
        )
        result = await submit(session, summary, [experience])
        again = await submit(session, "响应丢失后重复提交", [experience])
        assert again == result
        saved = await session.get(TaskSummary, result["summary_id"])
        assert saved.content_md == summary and "人工" in saved.content_md
        assert saved.generation == 1
        item = await session.get(KnowledgeItem, result["candidate_ids"][0])
        assert item.status == "candidate" and item.content_md == rule
        assert item.source_session_id == "ses_1"
        assert item.origin == "assistant"
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1


async def test_no_experience_is_valid(factory):
    async with factory() as session:
        result = await submit(session)
        assert result["candidate_ids"] == []
        assert await session.scalar(select(func.count()).select_from(TaskSummary)) == 1
        assert (await session.get(Task, "T1")).status == "SUCCEEDED"


async def test_atomic_submission_failure_can_retry(factory, monkeypatch):
    original = review.KnowledgeService.mcp_submit_experience
    calls = 0

    async def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("library unavailable")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(review.KnowledgeService, "mcp_submit_experience", fail_second)
    experiences = [review.ReviewExperience(title=str(i), content_md=f"判断处理验证 {i}", evidence=[
        {"message_id": "msg_002", "part_id": "prt_2", "evidence_type": "human_correction"},
    ]) for i in range(2)]
    async with factory() as session:
        with pytest.raises(RuntimeError):
            await submit(session, experiences=experiences)
        await session.rollback()
        assert (await session.get(Task, "T1")).status == "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(TaskSummary)) == 0
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 0
        result = await submit(session, experiences=experiences)
        assert len(result["candidate_ids"]) == 2


async def test_old_session_and_old_round_cannot_overwrite(factory):
    async with factory() as session:
        task = await session.get(Task, "T1")
        binding = await session.get(RuntimeSession, "ses_1")
        old_token = review.review_id(task, binding)
        await submit(session, "旧总结")
        task.last_message_id = "new_turn"
        binding.last_message_id = "new_final"
        await session.commit()
        with pytest.raises(ValueError, match="轮次"):
            await submit(session, token=old_token)
        await submit(session, "同 Session 新轮次总结")
        session.add(
            RuntimeSession(
                id="ses_2",
                task_id="T1",
                employee_id="E1",
                workspace="/new",
                environment={},
                status="SUCCEEDED",
                last_message_id="final_2",
            )
        )
        task.current_session_id = "ses_2"
        await session.commit()
        await submit(session, "新 Session 总结", source="ses_2")
        with pytest.raises(ValueError, match="旧 Session"):
            await submit(session, "延迟旧总结", token=old_token)
        summary = await session.scalar(select(TaskSummary))
        assert summary.content_md == "新 Session 总结" and summary.primary_session_id == "ses_2"


async def test_manual_confirmation_needs_no_summary(factory, monkeypatch):
    class IdleNative:
        async def session(self, *args): return {"id": "ses_1"}
        async def status(self, *args): return "idle"
        async def children(self, *args): return []
        async def questions(self, *args): return []
        async def permissions(self, *args): return []
        async def messages(self, *args):
            return [
                {"info": {"id": "msg_001", "role": "user"}, "parts": []},
                {"info": {"id": "msg_200", "role": "assistant", "finish": "stop",
                          "time": {"completed": 1_800_000_000_000}},
                 "parts": [{"type": "text", "text": "完成"}]},
            ]
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield IdleNative()
    monkeypatch.setattr("app.runtime.lifecycle.connect", connect)
    monkeypatch.setattr(
        "app.runtime.lifecycle.resolve_environment", AsyncMock(return_value=SimpleNamespace())
    )
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.status = "WAITING"
        task.workspace = "/bound"
        task.completion_policy = "MANUAL_CONFIRM"
        task.submitted_at = datetime.now(UTC)
        await session.commit()
        await RuntimeService(session).confirm_completion("T1")
        assert task.status == "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(TaskSummary)) == 0


async def test_long_session_reads_early_correction_and_question_answer(factory, monkeypatch):
    messages = [
        {
            "info": {"id": f"msg_{i:03}", "role": "user" if i == 1 else "assistant"},
            "parts": [{"id": f"prt_{i}", "type": "text", "text": "人工纠正" * 1000 if i == 1 else "工作"}],
        }
        for i in range(201)
    ]
    messages[2]["parts"] = [
        {
            "id": "question_part",
            "type": "tool",
            "tool": "question",
            "state": {
                "input": {"questions": ["需要保留吗？"]},
                "metadata": {"answers": [["保留全部历史"]]},
                "output": "保留全部历史",
            },
        }
    ]
    calls = []

    class Client:
        async def children(self, directory, sid):
            return [{"id": "child"}] if sid == "ses_1" else []

        async def messages_page(self, directory, sid, *, limit, before):
            calls.append((str(directory), sid, before))
            source = messages if sid == "ses_1" else []
            end = int(before.removeprefix("cursor:")) if before else len(source)
            start = max(0, end - limit)
            return {"messages": source[start:end], "next_before": f"cursor:{start}" if start else None}

    @asynccontextmanager
    async def connect(environment):
        yield Client()

    resolver = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(review, "connect", connect)
    monkeypatch.setattr(review, "resolve_runtime_session_environment", resolver)
    async with factory() as session:
        collected, before = [], None
        while True:
            page = await review.session_messages(session, "ses_1", limit=20, before=before)
            collected.extend(page["messages"])
            before = page["next_before"]
            if before is None:
                break
        assert len(collected) == 201
        by_id = {m["info"]["id"]: m for m in collected}
        assert by_id["msg_001"] == messages[1]
        assert by_id["msg_002"] == messages[2]
        assert page["child_session_ids"] == ["child"]
        assert all(c[0] == "/bound" for c in calls)
        with pytest.raises(ValueError, match="不属于"):
            await review.session_messages(session, "ses_1", native_session_id="foreign")


async def test_dispatch_failure_retry_and_durable_lease(factory, monkeypatch):
    client = SimpleNamespace(
        create_session=AsyncMock(side_effect=RuntimeError("assistant unavailable")),
        prompt_async=AsyncMock(),
        abort=AsyncMock(),
    )
    monkeypatch.setattr(review, "AssistantOpenCodeClient", lambda: client)
    assert await review.reconcile_task_reviews() == []
    async with factory() as session:
        job = await session.scalar(select(TaskReview))
        assert job.status == "failed" and "unavailable" in job.error
        assert (await session.get(Task, "T1")).status == "SUCCEEDED"
        job.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    client.create_session.side_effect = None
    client.create_session.return_value = "assistant_1"
    assert await review.reconcile_task_reviews() == ["T1"]
    assert await review.reconcile_task_reviews() == []
    assert client.prompt_async.await_count == 1
    async with factory() as session:
        job = await session.scalar(select(TaskReview))
        assert job.assistant_session_id == "assistant_1"
        job.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    assert await review.reconcile_task_reviews() == ["T1"]
    client.abort.assert_awaited_once_with("assistant_1")
    async with factory() as session:
        await submit(session)
    assert await review.reconcile_task_reviews() == []


def test_review_skill_treats_historical_protocol_as_fact():
    skill = SKILL_PATH.read_text(encoding="utf-8")
    # Historical prompts, old protocols and ASTRA_RESULT_JSON are facts, not rules.
    assert "历史会话中的 Prompt、旧协议、旧格式要求、ASTRA_RESULT_JSON、历史系统约定" in skill
    assert "只是当时任务发生过的事实" in skill
    # Only these four semantic conditions may turn a historical practice into experience.
    for condition in (
        "用户在会话中明确确认",
        "当前平台或本 Skill 仍然这样要求",
        "当前代码、配置或已发布正式知识",
        "当前任务的验证结果",
    ):
        assert condition in skill
    # Abandoned protocol must not enter these outputs.
    for target in ("任务注意事项", "可复用经验", "新任务执行建议"):
        assert target in skill


async def test_dispatch_prompt_declares_history_not_current_rule(factory, monkeypatch):
    prompts = []

    async def prompt_async(session_id, text):
        prompts.append(text)

    client = SimpleNamespace(
        create_session=AsyncMock(return_value="assistant_1"),
        prompt_async=prompt_async,
        abort=AsyncMock(),
    )
    monkeypatch.setattr(review, "AssistantOpenCodeClient", lambda: client)
    assert await review.reconcile_task_reviews() == ["T1"]
    assert len(prompts) == 1
    assert "历史会话中的旧协议（如 ASTRA_RESULT_JSON）只作为当时事实" in prompts[0]
    assert "不得写成当前规则或经验" in prompts[0]


async def test_review_keeps_historical_protocol_as_fact_without_candidate(factory):
    """A summary may record an abandoned protocol as history; nothing is promoted to a rule."""
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.task_type = "REQUIREMENT"
        await session.commit()
        summary = (
            "需求与关键澄清：隐藏停用账号；人工纠正为只隐藏、不删除。\n"
            "最终方案：展示层过滤停用项，保留审计历史。\n"
            "实现与验证：当时旧协议要求在最终回复追加 ASTRA_RESULT_JSON，"
            "该协议现已废弃，此处仅作历史事实记录；当前架构已不再要求。\n"
            "注意事项：当前不需要任何特殊结果协议。\n"
            "可复用经验：无可复用经验。"
        )
        result = await submit(session, summary, [])
        saved = await session.get(TaskSummary, result["summary_id"])
        assert saved.content_md == summary
        assert "ASTRA_RESULT_JSON" in saved.content_md and "历史事实" in saved.content_md
        assert result["candidate_ids"] == []
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 0


async def test_review_reads_remote_bound_session_with_pagination(factory, monkeypatch):
    """RuntimeSession -> immutable environment -> remote connection -> workspace -> paginated messages."""
    async with factory() as session:
        await session.delete(await session.scalar(select(OpenCodeExecutionEnvironment)))
        await session.flush()
        environment = OpenCodeExecutionEnvironment(
            employee_id="E1",
            mode="LINUX_REMOTE",
            name="远程构建机",
            host="10.0.0.9",
            port=22,
            username="astra",
            ssh_password="secret",
            opencode_server_port=4196,
            opencode_path="/opt/opencode",
            workspace_root="/srv/ws",
            status="AVAILABLE",
        )
        session.add(environment)
        await session.flush()
        binding = await session.get(RuntimeSession, "ses_1")
        binding.environment = environment_snapshot(environment)
        binding.workspace = "/srv/ws/T1"
        await session.commit()

    seen = {}
    client = SimpleNamespace(
        children=AsyncMock(return_value=[]),
        messages_page=AsyncMock(
            return_value={"messages": [{"info": {"id": "msg_2"}, "parts": []}], "next_before": None}
        ),
    )

    @asynccontextmanager
    async def connect(environment):
        # Credentials come from the RuntimeSession's immutable environment row.
        seen["mode"] = environment.mode
        seen["host"] = environment.host
        seen["ssh_password"] = environment.ssh_password
        yield client

    monkeypatch.setattr(review, "connect", connect)
    async with factory() as session:
        page = await review.session_messages(session, "ses_1", limit=20)
    assert seen["mode"] == ExecutionMode.LINUX_REMOTE
    assert seen["host"] == "10.0.0.9"
    assert seen["ssh_password"] == "secret"
    assert client.messages_page.await_args.args[0] == Path("/srv/ws/T1")
    assert page["messages"] == [{"info": {"id": "msg_2"}, "parts": []}]


def experience(**changes):
    return review.ReviewExperience(**{
        "title": "复用判断方法", "content_md": "先比较直连与缓存结果，再检查键构造，验证多租户隔离。",
        "evidence": [{"message_id": "msg_002", "part_id": "prt_2",
                      "evidence_type": "human_correction", "content": "人工纠正"}],
        **changes,
    })


@pytest.mark.parametrize("status", ["RUNNING", "WAITING", "FAILED", "CANCELED"])
async def test_unconfirmed_rounds_do_not_dispatch_or_submit(factory, monkeypatch, status):
    client = SimpleNamespace(create_session=AsyncMock(), prompt_async=AsyncMock())
    monkeypatch.setattr(review, "AssistantOpenCodeClient", lambda: client)
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.status = status
        await session.commit()
        with pytest.raises(ValueError, match="人工确认"):
            await submit(session)
    assert await review.reconcile_task_reviews() == []
    client.create_session.assert_not_awaited()


@pytest.mark.parametrize("evidence,error", [
    ([], "真实执行证据"),
    ([{"message_id": "invented", "evidence_type": "message"}], "消息不存在"),
    ([{"message_id": "msg_002", "part_id": "invented", "evidence_type": "message"}], "片段不存在"),
    ([{"message_id": "msg_002", "evidence_type": "message", "content": "伪造结果"}], "摘录"),
    ([{"message_id": "msg_002", "part_id": "prt_2", "evidence_type": "verification",
       "result": "passed", "content": "人工纠正"}], "工具结果"),
])
async def test_evidence_is_checked_before_atomic_save(factory, evidence, error):
    async with factory() as session:
        with pytest.raises(ValueError, match=error):
            await submit(session, experiences=[experience(evidence=evidence)])
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(TaskSummary)) == 0
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 0


@pytest.mark.parametrize("verified", [False, True])
async def test_task_success_is_not_experience_verification(factory, verified):
    proof = [{"message_id": "msg_test", "part_id": "prt_test", "evidence_type": "verification",
              "result": "passed", "content": "2 passed"}]
    async with factory() as session:
        result = await submit(session, experiences=[experience(**({"evidence": proof} if verified else {}))])
        item = await session.get(KnowledgeItem, result["candidate_ids"][0])
        assert item.verification_status == ("verified" if verified else "unverified")
        assert item.status == "candidate"
        assert item.verification_message_id == ("msg_test" if verified else None)


async def test_more_than_two_experiences_need_independent_value(factory):
    async with factory() as session:
        experiences = [experience(title=str(i), content_md=f"独立方法 {i}") for i in range(3)]
        with pytest.raises(ValueError, match="独立复用价值"):
            await submit(session, experiences=experiences)
        for i, item in enumerate(experiences):
            item.independent_value = f"适用于独立场景 {i}"
        result = await submit(session, experiences=experiences)
        assert len(result["candidate_ids"]) == 3


async def second_task(session):
    binding = await session.get(RuntimeSession, "ses_1")
    task = Task(id="T2", title="另一个缓存任务", task_type="ISSUE", status="SUCCEEDED",
                employee_id="E1", current_session_id="ses_2", last_message_id="msg_001")
    second = RuntimeSession(id="ses_2", task_id="T2", employee_id="E1", workspace="/bound",
                            environment=binding.environment, status="SUCCEEDED", last_message_id="msg_200")
    session.add_all([task, second])
    await session.commit()
    return task, second


async def test_semantic_merge_retains_evidence_and_links_both_tasks(factory):
    from app.assistant.tool_gateway import experience_search
    from app.models import ExperienceEvidence

    async with factory() as session:
        first = await submit(session, experiences=[experience()])
        target = first["candidate_ids"][0]
        task, binding = await second_task(session)
        found = await experience_search(session, keyword="缓存")
        assert [i["id"] for i in found["items"]] == [target]
        result = await review.submit_review(
            session, task_id=task.id, source_session_id=binding.id,
            expected_review_id=review.review_id(task, binding), task_summary="另一任务的客观总结",
            experiences=[experience(merge_into_id=target, merge_reason="同一缓存键判断与多租户验证方法",
                                    content_md="合并后：先隔离直连与缓存路径，再统一键构造并验证多租户。")],
        )
        assert result["candidate_ids"] == [target]
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
        assert await session.scalar(select(func.count()).select_from(ExperienceEvidence)) == 2
        for task_id in ("T1", "T2"):
            detail = await review.review_detail(session, await session.get(Task, task_id))
            assert [i["id"] for i in detail["experiences"]] == [target]


async def test_published_experience_reuse_only_appends_evidence(factory):
    from app.models import ExperienceEvidence

    async with factory() as session:
        first = await submit(session, experiences=[experience()])
        item = await session.get(KnowledgeItem, first["candidate_ids"][0])
        item.status, item.verification_status = "published", "verified"
        content, fingerprint = item.content_md, item.fingerprint
        await session.commit()
        task, binding = await second_task(session)
        result = await review.submit_review(
            session, task_id=task.id, source_session_id=binding.id,
            expected_review_id=review.review_id(task, binding), task_summary="复用了既有正式知识",
            experiences=[experience(merge_into_id=item.id, merge_reason="方法一致，补充证据",
                                    content_md="模型不能替换这段正式正文")],
        )
        assert result["candidate_ids"] == [item.id]
        await session.refresh(item)
        assert (item.content_md, item.fingerprint, item.status) == (content, fingerprint, "published")
        assert item.verification_status == "verified"
        assert await session.scalar(select(func.count()).select_from(ExperienceEvidence)) == 2


async def test_failed_review_retry_reuses_record_and_completed_retry_is_noop(factory):
    async with factory() as session:
        task = await session.get(Task, "T1")
        binding = await session.get(RuntimeSession, "ses_1")
        key = review.review_id(task, binding)
        session.add(TaskReview(id=key, task_id=task.id, source_session_id=binding.id,
                               source_status=task.status, status="failed", error="网络中断", attempts=1))
        await session.commit()
        detail = await review.retry_review(session, task.id)
        assert detail["status"] == "pending" and detail["error"] is None
        assert await session.scalar(select(func.count()).select_from(TaskReview)) == 1
        await submit(session)
        assert (await review.retry_review(session, task.id))["status"] == "completed"
        assert await session.scalar(select(func.count()).select_from(TaskSummary)) == 1


async def test_evidence_read_race_cannot_write_old_round(factory, monkeypatch):
    original = review.validate_evidence

    async def reopen(*args):
        verified = await original(*args)
        async with factory() as other:
            task = await other.get(Task, "T1")
            task.last_message_id = "new_turn"
            await other.commit()
        return verified

    monkeypatch.setattr(review, "validate_evidence", reopen)
    async with factory() as session:
        with pytest.raises(ValueError, match="轮次"):
            await submit(session, experiences=[experience()])
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 0


def test_review_is_registered_in_real_beat_and_control_worker():
    from app.runtime.celery_app import celery

    entry = celery.conf.beat_schedule["nexus-task-review"]
    assert entry["task"] in celery.tasks
    assert celery.conf.task_routes[entry["task"]]["queue"] == "nexus-control"


async def test_confirmation_syncs_native_root_before_review_dispatch(factory, monkeypatch):
    native = [
        {"info": {"id": "msg_001", "role": "user"}, "parts": []},
        {"info": {"id": "msg_200", "role": "assistant", "finish": "stop",
                  "time": {"completed": 1_800_000_000_000}},
         "parts": [{"id": "final", "type": "text", "text": "已完成"}]},
    ]

    @asynccontextmanager
    async def native_connection(*args, **kwargs):
        yield SimpleNamespace(
            session=AsyncMock(return_value={"id": "ses_1"}), status=AsyncMock(return_value="idle"),
            children=AsyncMock(return_value=[]), questions=AsyncMock(return_value=[]),
            permissions=AsyncMock(return_value=[]), messages=AsyncMock(return_value=native),
        )

    monkeypatch.setattr("app.runtime.lifecycle.connect", native_connection)
    monkeypatch.setattr(
        "app.runtime.lifecycle.resolve_environment", AsyncMock(return_value=SimpleNamespace()),
    )
    client = SimpleNamespace(create_session=AsyncMock(return_value="review_1"), prompt_async=AsyncMock())
    monkeypatch.setattr(review, "AssistantOpenCodeClient", lambda: client)
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.status, task.workspace = "RUNNING", "/bound"
        binding = await session.get(RuntimeSession, "ses_1")
        binding.status, binding.last_message_id = "RUNNING", "msg_001"
        await session.commit()
        assert await review.reconcile_task_reviews() == []
        await RuntimeService(session).confirm_completion(task.id)
        assert task.status == "SUCCEEDED" and task.confirmed_at
        assert binding.status == "SUCCEEDED" and binding.last_message_id == "msg_200"
    assert await review.reconcile_task_reviews() == ["T1"]
    assert await review.reconcile_task_reviews() == []


async def test_expired_review_can_retry_but_live_lease_cannot(factory):
    async with factory() as session:
        task = await session.get(Task, "T1")
        binding = await session.get(RuntimeSession, "ses_1")
        job = TaskReview(id=review.review_id(task, binding), task_id=task.id,
                         source_session_id=binding.id, source_status=task.status,
                         status="running", retry_at=datetime.now(UTC) + timedelta(minutes=1))
        session.add(job)
        await session.commit()
        with pytest.raises(ValueError, match="仍在执行"):
            await review.retry_review(session, task.id)
        job.retry_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
        detail = await review.review_detail(session, task)
        assert detail["status"] == "failed" and detail["can_retry"] and "超时" in detail["error"]
        assert (await review.retry_review(session, task.id))["status"] == "pending"


async def test_candidate_content_change_revokes_previous_verification(factory):
    async with factory() as session:
        proof = [{"message_id": "msg_test", "part_id": "prt_test", "evidence_type": "verification",
                  "result": "passed", "content": "2 passed"}]
        first = await submit(session, experiences=[experience(evidence=proof)])
        target = first["candidate_ids"][0]
        assert (await session.get(KnowledgeItem, target)).verification_status == "verified"
        task, binding = await second_task(session)
        await review.submit_review(
            session, task_id=task.id, source_session_id=binding.id,
            expected_review_id=review.review_id(task, binding), task_summary="新增未验证边界",
            experiences=[experience(merge_into_id=target, merge_reason="补充同一方法的边界",
                                    content_md="含新边界的处理方法，尚未验证新增范围。")],
        )
        item = await session.get(KnowledgeItem, target)
        assert item.verification_status == "unverified" and item.verified_at is None


async def test_mcp_returns_actionable_validation_error(factory, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from app.assistant import mcp_server

    monkeypatch.setattr(mcp_server, "SessionLocal", factory)
    monkeypatch.setattr(mcp_server, "submit_review", AsyncMock(side_effect=ValueError("经验证据摘录不符")))
    server = mcp_server.build_assistant_mcp_server()
    with pytest.raises(ToolError, match="经验证据摘录不符"):
        await server.call_tool("nexus_task_review_submit", {
            "task_id": "T1", "source_session_id": "ses_1", "review_id": "round",
            "task_summary": "任务总结", "experiences": [],
        })


async def test_failed_command_cannot_be_marked_verified(factory, monkeypatch):
    @asynccontextmanager
    async def native_connection(*args, **kwargs):
        yield SimpleNamespace(children=AsyncMock(return_value=[]), messages_page=AsyncMock(return_value={
            "messages": [{"info": {"id": "failed", "role": "assistant"}, "parts": [
                {"id": "part", "type": "tool", "tool": "bash", "state": {
                    "status": "completed", "metadata": {"exit": 1}, "output": "1 failed",
                }},
            ]}], "next_before": None,
        }))

    monkeypatch.setattr(review, "connect", native_connection)
    async with factory() as session:
        with pytest.raises(ValueError, match="失败的工具执行"):
            await submit(session, experiences=[experience(evidence=[{
                "message_id": "failed", "part_id": "part", "evidence_type": "verification",
                "result": "passed", "content": "1 failed",
            }])])


async def test_review_capture_failure_does_not_block_human_completion(factory, monkeypatch):
    @asynccontextmanager
    async def native_connection(*args, **kwargs):
        yield SimpleNamespace(
            session=AsyncMock(return_value={"id": "ses_1"}), status=AsyncMock(return_value="idle"),
            children=AsyncMock(return_value=[]), questions=AsyncMock(return_value=[]),
            permissions=AsyncMock(return_value=[]),
            messages=AsyncMock(side_effect=ConnectionError("读取失败")),
        )

    monkeypatch.setattr("app.runtime.lifecycle.connect", native_connection)
    monkeypatch.setattr(
        "app.runtime.lifecycle.resolve_environment", AsyncMock(return_value=SimpleNamespace()),
    )
    async with factory() as session:
        task = await session.get(Task, "T1")
        task.status, task.workspace = "RUNNING", "/bound"
        binding = await session.get(RuntimeSession, "ses_1")
        binding.status = "UNKNOWN"
        await session.commit()
        with pytest.raises(RuntimeStateError, match="无法确认"):
            await RuntimeService(session).confirm_completion(task.id)
        await session.refresh(task)
        await session.refresh(binding)
        assert task.status == "RUNNING"
        assert binding.status == "UNKNOWN"
        binding.status = "SUCCEEDED"
        task.status = "WAITING"
        await session.commit()
        await RuntimeService(session).confirm_completion(task.id)
        assert task.status == "SUCCEEDED" and task.confirmed_at
        assert binding.status == "SUCCEEDED"
        assert review.review_ready(task, binding)
