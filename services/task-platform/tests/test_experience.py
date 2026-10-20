"""Experience identity is global; verification requires cited execution evidence."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.library_schemas import LibrarySubmitExperienceInput
from app.library_service import KnowledgeService, LibraryPrincipal
from app.models import (
    AgentWorker,
    Base,
    ExperienceEvidence,
    KnowledgeItem,
    KnowledgeSpace,
    RuntimeSession,
    Task,
)


@asynccontextmanager
async def experience_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(AgentWorker(id="E1", name="员工", role="build"))
        for index in (1, 2):
            session.add(Task(id=f"T{index}", title="任务", task_type="ISSUE", status="SUCCEEDED",
                             employee_id="E1", current_session_id=f"ses_{index}"))
            session.add(RuntimeSession(id=f"ses_{index}", task_id=f"T{index}", employee_id="E1",
                                       workspace="/test", environment={}, status="SUCCEEDED"))
        await session.commit()
        yield session, "unused"
    await engine.dispose()


async def test_exact_experience_is_reused_across_tasks_and_titles():
    async with experience_session() as (session, _):
        service = KnowledgeService(session)
        ids = []
        for index in (1, 2):
            item = await service.mcp_submit_experience(
                LibrarySubmitExperienceInput(title=f"标题{index}", content_md="同一判断处理验证方法",
                                             evidence=[
                    {"evidence_type": "message", "message_id": f"msg_{index}"},
                ]), review_session=await session.get(RuntimeSession, f"ses_{index}"), review_key=str(index),
            )
            ids.append(item.id)
        assert ids[0] == ids[1]
        assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
        assert await session.scalar(select(func.count()).select_from(ExperienceEvidence)) == 2
        assert item.verification_status == "unverified"


async def test_task_experience_is_filed_under_its_category_space():
    async with experience_session() as (session, _):
        session.add(KnowledgeSpace(id="SPACE_TESTING", name="测试领域知识", status="active"))
        session.add(KnowledgeSpace(id="SPACE_PUBLIC", name="公共知识库", status="active"))
        task = await session.get(Task, "T1")
        task.category_id = "testing-platform"
        await session.commit()
        service = KnowledgeService(session)
        item = await service.mcp_submit_experience(
            LibrarySubmitExperienceInput(
                title="登录重试",
                content_md="接口超时后先查网关再重试",
                evidence=[{"evidence_type": "message", "message_id": "msg_space"}],
            ),
            review_session=await session.get(RuntimeSession, "ses_1"),
            review_key="space",
        )
        assert item.source_type == "task"
        assert item.source_task_id == "T1"
        assert item.space_id == "SPACE_TESTING"
        assert item.space_name == "测试领域知识"
        spaces = await service.list_spaces(LibraryPrincipal())
        testing = next(space for space in spaces if space.id == "SPACE_TESTING")
        public = next(space for space in spaces if space.id == "SPACE_PUBLIC")
        assert testing.knowledge_count == 1
        assert public.knowledge_count == 0


async def test_retry_does_not_overwrite_published_experience():
    async with experience_session() as (session, _):
        service = KnowledgeService(session)
        item = await service.mcp_submit_experience(
            LibrarySubmitExperienceInput(title="已发布标题", content_md="正式正文"),
            review_session=await session.get(RuntimeSession, "ses_1"),
            review_key="published",
        )
        stored = await session.get(KnowledgeItem, item.id)
        stored.status = "published"
        await session.commit()
        again = await service.mcp_submit_experience(
            LibrarySubmitExperienceInput(title="改写标题", content_md="另一套正文"),
            review_session=await session.get(RuntimeSession, "ses_2"),
            review_key="retry",
            merge_into_id=item.id,
            merge_reason="同一方法",
        )
        assert again.id == item.id
        assert again.title == "已发布标题"
        assert again.content_md == "正式正文"
        assert again.status == "published"


async def test_manual_experience_is_unverified_and_deduped():
    async with experience_session() as (session, _):
        service = KnowledgeService(session)
        payload = LibrarySubmitExperienceInput(title="人工经验", content_md="同样内容")
        first = await service.mcp_submit_experience(payload)
        second = await service.mcp_submit_experience(payload)
        assert first.id == second.id
        assert first.verification_status == "unverified"


def test_published_knowledge_survives_task_deletion():
    from app.database import SessionLocal
    from app.main import app
    from app.models import AgentWorker, Task

    suffix = uuid4().hex[:8]
    employee_id = f"E-DEL-{suffix}"
    task_id = f"T-DEL-{suffix}"
    knowledge_id = f"KNOW-DEL-{suffix}"

    async def setup():
        async with SessionLocal() as session:
            session.add(AgentWorker(id=employee_id, name="Del", role="build", status="IDLE"))
            session.add(
                Task(
                    id=task_id,
                    title="待删除任务",
                    task_type="ISSUE",
                    status="SUCCEEDED",
                    employee_id=employee_id,
                )
            )
            session.add(
                KnowledgeItem(
                    id=knowledge_id,
                    title="长期经验",
                    type="experience",
                    summary="摘要",
                    content_md="内容",
                    status="published",
                    verification_status="verified",
                    created_by="reviewer",
                    updated_by="reviewer",
                    source_task_id=task_id,
                    source_ref_snapshot={"task_title": "待删除任务"},
                )
            )
            await session.commit()

    async def read():
        async with SessionLocal() as session:
            item = await session.get(KnowledgeItem, knowledge_id)
            return item.source_task_id, item.source_ref_snapshot

    with TestClient(app) as client:
        asyncio.run(setup())
        assert client.delete(f"/api/tasks/{task_id}").status_code == 200
    source_task_id, snapshot = asyncio.run(read())
    assert source_task_id is None
    assert snapshot == {"task_title": "待删除任务"}


async def test_usage_completed_tasks_share_experience_completion_definition():
    from app.domain import TaskStatus
    from app.usage import COMPLETED_TASK_STATUS, summarize_sessions

    async with experience_session() as (session, _token):
        task = await session.get(Task, "T1")
        task.status = "SUCCEEDED"
        task.completed_at = datetime.now(UTC)
        runtime_session = await session.get(RuntimeSession, "ses_1")
        runtime_session.status = "SUCCEEDED"
        await session.commit()

        usage = await summarize_sessions(session, [runtime_session])

        assert COMPLETED_TASK_STATUS == TaskStatus.SUCCEEDED
        assert usage["completed_tasks"] == 1


async def test_human_content_edit_refreshes_identity_and_verification():
    from app.library_schemas import KnowledgePatch
    from app.library_service import LibraryPrincipal, experience_fingerprint

    async with experience_session() as (session, _):
        service = KnowledgeService(session)
        saved = await service.mcp_submit_experience(LibrarySubmitExperienceInput(
            title="原有方法", content_md="原有方法正文",
        ))
        item = await session.get(KnowledgeItem, saved.id)
        item.verification_status = "verified"
        await session.commit()
        updated = await service.update_knowledge(
            item.id, KnowledgePatch(content_md="新增未验证边界"),
            LibraryPrincipal(user_id="admin", role="admin"),
        )
        assert updated.verification_status == "unverified"
        assert item.fingerprint == experience_fingerprint("新增未验证边界")
        different = await service.mcp_submit_experience(LibrarySubmitExperienceInput(
            title="原有方法的新证据", content_md="原有方法正文",
        ))
        assert different.id != item.id


def test_exact_fingerprint_preserves_code_case_and_indentation():
    from app.library_service import experience_fingerprint

    assert experience_fingerprint("assert Foo") != experience_fingerprint("assert foo")
    assert experience_fingerprint("if ok:\n    run()") != experience_fingerprint("if ok:\nrun()")
    assert experience_fingerprint("规则\r\n方法") == experience_fingerprint("规则\n方法")


async def test_concurrent_exact_submissions_share_one_record(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def save(title):
        async with factory() as session:
            return (await KnowledgeService(session).mcp_submit_experience(
                LibrarySubmitExperienceInput(title=title, content_md="同一可复用判断与验证方法"),
            )).id

    try:
        ids = await asyncio.gather(save("任务甲的提炼"), save("任务乙的提炼"))
        assert ids[0] == ids[1]
        async with factory() as session:
            assert await session.scalar(select(func.count()).select_from(KnowledgeItem)) == 1
    finally:
        await engine.dispose()
