from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.library_schemas import LibraryResource, LibrarySubmitExperienceInput
from app.library_service import KnowledgeService
from app.models import (
    AgentWorker,
    Base,
    KnowledgeItem,
    KnowledgeUsage,
    OpenCodeExecutionEnvironment,
    RuntimeSession,
    Task,
)
from app.runtime.environment import environment_snapshot
from app.runtime.opencode_workspace import discover_workspace_config
from app.runtime.runtime_context import (
    RuntimeContextError,
    generate_runtime_context,
    hash_runtime_context,
    resolve_runtime_context,
    runtime_context_id,
)


@asynccontextmanager
async def context_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        employee = AgentWorker(id="E1", name="Engineer", role="build", status="IDLE")
        environment = OpenCodeExecutionEnvironment(
            employee_id="E1", mode="LOCAL", name="test", workspace_root="/tmp/ws", status="AVAILABLE"
        )
        session.add_all([employee, environment])
        await session.flush()
        task = Task(
            id="T1",
            title="Task",
            task_type="ISSUE",
            status="RUNNING",
            employee_id="E1",
            current_session_id="ses_a",
            workspace="/tmp/ws",
            execution_environment_snapshot=environment_snapshot(environment),
        )
        token = generate_runtime_context()
        digest = hash_runtime_context(token)
        session.add_all(
            [
                task,
                RuntimeSession(
                    id="ses_a",
                    task_id="T1",
                    employee_id="E1",
                    workspace="/tmp/ws",
                    environment=environment_snapshot(environment),
                    status="RUNNING",
                    runtime_context_hash=digest,
                    runtime_context_id=runtime_context_id(digest),
                ),
            ]
        )
        await session.commit()
        yield session, token
    await engine.dispose()


async def test_correct_token_resolves_origin():
    async with context_session() as (session, token):
        context = await resolve_runtime_context(session, token)
    assert context.task_id == "T1"
    assert context.session_id == "ses_a"
    assert context.employee_id == "E1"
    assert context.runtime_context_id == hash_runtime_context(token)[:12]


async def test_missing_and_wrong_tokens_are_rejected():
    async with context_session() as (session, _token):
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(session, None)
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(session, "ctx_not-a-real-token")
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(session, "ses_a")


async def test_deleted_session_token_is_rejected():
    async with context_session() as (session, token):
        row = await session.get(RuntimeSession, "ses_a")
        await session.delete(row)
        await session.commit()
        with pytest.raises(RuntimeContextError):
            await resolve_runtime_context(session, token)


async def test_old_session_token_resolves_old_not_current():
    async with context_session() as (session, token_a):
        token_b = generate_runtime_context()
        digest_b = hash_runtime_context(token_b)
        session.add(
            RuntimeSession(
                id="ses_b",
                task_id="T1",
                employee_id="E1",
                workspace="/tmp/ws",
                environment={},
                status="RUNNING",
                runtime_context_hash=digest_b,
                runtime_context_id=runtime_context_id(digest_b),
            )
        )
        task = await session.get(Task, "T1")
        task.current_session_id = "ses_b"
        old = await session.get(RuntimeSession, "ses_a")
        old.superseded_by = "ses_b"
        await session.commit()

        context = await resolve_runtime_context(session, token_a)
        assert context.session_id == "ses_a"

        current = await resolve_runtime_context(session, token_b)
        assert current.session_id == "ses_b"


async def test_employee_cannot_use_removed_runtime_experience_submission():
    async with context_session() as (session, token):
        with pytest.raises(TypeError, match="runtime_context"):
            await KnowledgeService(session).mcp_submit_experience(
                LibrarySubmitExperienceInput(title="员工经验", content_md="处理规则"),
                runtime_context=token,
            )


async def test_runtime_library_read_records_task_session_and_knowledge(monkeypatch):
    async with context_session() as (session, token):
        item = KnowledgeItem(
            id="KNOW_READ_1",
            title="参数升级规则",
            type="rule",
            summary="升级规则",
            content_md="# 参数升级规则",
            status="published",
            verification_status="verified",
            tags=[],
            origin="manual",
            scope={},
            source_ref_snapshot={},
            experience_data={},
            created_by="test",
            updated_by="test",
        )
        session.add(item)
        await session.commit()

        async def fake_read(_client, resource_id):
            return LibraryResource(
                resource_id=resource_id,
                path=resource_id,
                content_md=(
                    "---\n"
                    "astra_knowledge_id: KNOW_READ_1\n"
                    "---\n\n"
                    "# 参数升级规则"
                ),
            )

        monkeypatch.setattr("app.library_service.LlmWikiClient.read", fake_read)
        await KnowledgeService(session).mcp_get(
            "wiki/parameter-upgrade.md",
            runtime_context=token,
        )

        usage = await session.scalar(
            select(KnowledgeUsage).where(KnowledgeUsage.action == "read")
        )
        assert usage.knowledge_id == "KNOW_READ_1"
        assert usage.employee_id == "E1"
        assert usage.task_id == "T1"
        assert usage.session_id == "ses_a"


async def test_invalid_token_never_echoes_the_token(caplog):
    secret = "ctx_supersecretvalue"
    async with context_session() as (session, _token):
        with caplog.at_level("DEBUG"):
            with pytest.raises(RuntimeContextError) as error:
                await resolve_runtime_context(session, secret)
    assert secret not in str(error.value)
    assert secret not in caplog.text


def test_workspace_config_is_discovered_from_nested_directory(tmp_path):
    workspace = tmp_path / "workspaces" / "astra-01"
    workspace.mkdir(parents=True)
    (tmp_path / "workspaces" / "opencode.json").write_text("{}", encoding="utf-8")
    assert discover_workspace_config(workspace) == tmp_path / "workspaces" / "opencode.json"


def test_workspace_config_is_not_inherited_above_a_git_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    task_worktree = repo / "tasks" / "T1"
    task_worktree.mkdir(parents=True)
    (tmp_path / "opencode.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("app.runtime.opencode_workspace.global_config_path", lambda: None)
    # A per-task git worktree is its own project root: configs above it must not leak in.
    assert discover_workspace_config(task_worktree) is None


def test_missing_workspace_config_falls_back_to_global(monkeypatch, tmp_path):
    workspace = tmp_path / "workspaces" / "astra-01"
    workspace.mkdir(parents=True)
    global_config = tmp_path / "global-opencode.json"
    global_config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "app.runtime.opencode_workspace.global_config_path", lambda: global_config
    )
    assert discover_workspace_config(workspace) == global_config


async def test_runtime_session_context_hash_is_unique():
    async with context_session() as (session, token):
        digest = hash_runtime_context(token)
        duplicate = await session.scalar(
            select(RuntimeSession).where(RuntimeSession.runtime_context_hash == digest)
        )
        assert duplicate is not None
