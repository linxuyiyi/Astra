import json
import os
import sqlite3
import subprocess
import sys
from functools import partial
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import Settings
from app.library_schemas import (
    KnowledgeCreate,
    KnowledgePatch,
    KnowledgePublishWrite,
    KnowledgeSpaceCreate,
    KnowledgeSpacePatch,
    LibraryFeedbackInput,
    LibrarySearchInput,
    LibrarySubmitExperienceInput,
)
from app.library_service import KnowledgeService, LibraryPrincipal
from app.llm_wiki import LlmWikiClient, LlmWikiError, knowledge_source_path
from app.models import (
    AgentWorker,
    Base,
    KnowledgeFeedback,
    KnowledgeItem,
    KnowledgeUsage,
    RuntimeSession,
    Task,
)
from app.runtime.runtime_context import (
    generate_runtime_context,
    hash_runtime_context,
    runtime_context_id,
)


def wiki_resource(full_path: str) -> str:
    return f"wiki/sources/{full_path.rsplit('/', 1)[-1]}"


@pytest.fixture
async def wiki_env(tmp_path, monkeypatch):
    settings = Settings(
        _env_file=None,
        llm_wiki_enabled=True,
        llm_wiki_project_id="project-1",
        llm_wiki_api_token="test-secret",
        runtime_dir=tmp_path / "run",
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    state = {"status": 200, "calls": [], "sources": {}, "on_put": None}

    async def respond(request):
        state["calls"].append(request)
        assert request.headers["Authorization"] == "Bearer test-secret"
        if isinstance(state["status"], Exception):
            raise state["status"]
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"ok": False, "error": "test-secret"})
        path = request.url.path
        if path.endswith("/health"):
            return httpx.Response(200, json={"ok": True, "status": "running"})
        if path.endswith("/sources/file"):
            api_path = request.url.params["path"] if request.method == "DELETE" else None
            if request.method == "PUT":
                body = json.loads(request.content)
                full_path = "raw/sources/" + body["path"]
                existed = full_path in state["sources"]
                state["sources"][full_path] = body["content"]
                if state["on_put"]:
                    await state["on_put"](full_path)
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "path": full_path,
                        "action": "updated" if existed else "created",
                    },
                )
            full_path = "raw/sources/" + api_path
            if full_path not in state["sources"]:
                return httpx.Response(404, json={"ok": False, "error": "Source file not found"})
            del state["sources"][full_path]
            return httpx.Response(200, json={"ok": True, "path": full_path, "action": "deleted"})
        if path.endswith("/search"):
            pages = {wiki_resource(full): content for full, content in state["sources"].items()}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "results": [
                        {"path": resource, "title": "知识", "snippet": content[-100:], "score": 1.25}
                        for resource, content in pages.items()
                    ],
                },
            )
        if path.endswith("/files/content"):
            resource = request.url.params["path"]
            pages = {wiki_resource(full): content for full, content in state["sources"].items()}
            if resource not in pages:
                return httpx.Response(404)
            return httpx.Response(200, json={"path": resource, "content": pages[resource]})
        raise AssertionError(path)

    monkeypatch.setattr(
        httpx, "AsyncClient", partial(httpx.AsyncClient, transport=httpx.MockTransport(respond))
    )
    monkeypatch.setattr("app.llm_wiki.get_settings", lambda: settings)
    monkeypatch.setattr("app.library_service.get_settings", lambda: settings)
    token = generate_runtime_context()
    digest = hash_runtime_context(token)
    state["runtime_context"] = token
    async with sessions() as session:
        session.add(AgentWorker(id="E-wiki", name="Wiki", role="build", status="IDLE"))
        session.add(
            Task(
                id="T-wiki",
                title="Wiki",
                task_type="ISSUE",
                status="RUNNING",
                employee_id="E-wiki",
                current_session_id="ses_wiki",
                workspace="/tmp/wiki-ws",
                execution_environment_snapshot={},
            )
        )
        session.add(
            RuntimeSession(
                id="ses_wiki",
                task_id="T-wiki",
                employee_id="E-wiki",
                workspace="/tmp/wiki-ws",
                environment={},
                status="RUNNING",
                runtime_context_hash=digest,
                runtime_context_id=runtime_context_id(digest),
            )
        )
        service = KnowledgeService(session)
        space = await service.create_space(KnowledgeSpaceCreate(name="Public"), LibraryPrincipal())
        await session.commit()
    yield settings, sessions, state, space.id
    await engine.dispose()


async def publish(sessions, space_id, title="timeout", content="original body"):
    async with sessions() as session:
        service = KnowledgeService(session)
        item = await service.create_knowledge(
            KnowledgeCreate(space_id=space_id, title=title, content_md=content),
            LibraryPrincipal(),
        )
        await service.publish_knowledge(
            item.id, KnowledgePublishWrite(space_id=space_id), LibraryPrincipal()
        )
        return item.id


async def test_publish_puts_source_and_marks_synced(wiki_env):
    settings, sessions, state, space_id = wiki_env
    knowledge_id = await publish(sessions, space_id)
    expected = knowledge_source_path(knowledge_id)
    assert expected.endswith(f"astra/knowledge/{knowledge_id}.md")
    assert expected in state["sources"]
    async with sessions() as session:
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.status == "published"
        assert item.sync_status == "synced" and item.sync_error is None
        assert item.source_path == expected and item.synced_at is not None
    puts = [call for call in state["calls"] if call.method == "PUT"]
    assert len(puts) == 1


async def test_title_change_updates_same_source_path(wiki_env):
    settings, sessions, state, space_id = wiki_env
    knowledge_id = await publish(sessions, space_id, title="旧标题")
    expected = knowledge_source_path(knowledge_id)
    async with sessions() as session:
        service = KnowledgeService(session)
        await service.update_knowledge(
            knowledge_id, KnowledgePatch(title="新标题"), LibraryPrincipal()
        )
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.sync_status == "synced" and item.source_path == expected
    puts = [call for call in state["calls"] if call.method == "PUT"]
    assert len(puts) == 2
    bodies = [json.loads(call.content) for call in puts]
    assert {body["path"] for body in bodies} == {expected.removeprefix("raw/sources/")}
    assert "新标题" in state["sources"][expected]


async def test_archive_deletes_source_and_marks_synced(wiki_env):
    settings, sessions, state, space_id = wiki_env
    knowledge_id = await publish(sessions, space_id)
    expected = knowledge_source_path(knowledge_id)
    async with sessions() as session:
        service = KnowledgeService(session)
        await service.archive_knowledge(knowledge_id, LibraryPrincipal())
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.status == "archived" and item.sync_status == "synced"
    assert expected not in state["sources"]
    assert any(call.method == "DELETE" for call in state["calls"])


async def test_archive_of_missing_source_is_idempotent(wiki_env):
    settings, sessions, state, space_id = wiki_env
    knowledge_id = await publish(sessions, space_id)
    expected = knowledge_source_path(knowledge_id)
    del state["sources"][expected]
    async with sessions() as session:
        service = KnowledgeService(session)
        await service.archive_knowledge(knowledge_id, LibraryPrincipal())
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.status == "archived" and item.sync_status == "synced"


@pytest.mark.parametrize(
    "status, message",
    [
        (401, "Token"),
        (404, "不存在"),
        (500, "HTTP 500"),
        (httpx.ReadTimeout("test-secret"), "超时"),
        (httpx.ConnectError("test-secret"), "无法连接"),
    ],
)
async def test_sync_failure_never_rolls_back_publication(wiki_env, status, message):
    settings, sessions, state, space_id = wiki_env
    state["status"] = status
    knowledge_id = await publish(sessions, space_id)
    async with sessions() as session:
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.status == "published"
        assert item.sync_status == "failed"
        assert item.sync_error and message in item.sync_error
        assert "test-secret" not in item.sync_error
        assert item.synced_at is None
        assert state["sources"] == {}
        # A public read still fails cleanly while the service is down.
        with pytest.raises(LlmWikiError, match=message):
            await KnowledgeService(session).mcp_search(
                LibrarySearchInput(query="original"), runtime_context=state["runtime_context"]
            )
        # Recovery: resync turns the same published record into SYNCED.
        state["status"] = 200
        detail = await KnowledgeService(session).resync_knowledge(knowledge_id, LibraryPrincipal())
        assert detail.sync_status == "synced" and detail.status == "published"
        item = await session.get(KnowledgeItem, knowledge_id)
        assert item.sync_status == "synced" and item.sync_error is None and item.synced_at is not None


async def test_sync_failure_does_not_affect_task_or_experience(wiki_env):
    settings, sessions, state, space_id = wiki_env
    state["status"] = 500
    async with sessions() as session:
        service = KnowledgeService(session)
        candidate = await service.mcp_submit_experience(
            LibrarySubmitExperienceInput(title="流水线环境初始化超时", content_md="检查初始化日志")
        )
        assert candidate.status == "candidate"
        await service.publish_knowledge(
            candidate.id, KnowledgePublishWrite(space_id=space_id), LibraryPrincipal()
        )
        item = await session.get(KnowledgeItem, candidate.id)
        task = await session.get(Task, "T-wiki")
        session_row = await session.get(RuntimeSession, "ses_wiki")
        assert item.status == "published" and item.sync_status == "failed"
        assert task.status == "RUNNING" and session_row.status == "RUNNING"


async def test_lifecycle_and_resource_usage_records_runtime_identity(wiki_env):
    settings, sessions, state, space_id = wiki_env
    runtime_context = state["runtime_context"]
    async with sessions() as session:
        service = KnowledgeService(session)
        candidate = await service.mcp_submit_experience(
            LibrarySubmitExperienceInput(title="流水线环境初始化超时", content_md="检查初始化日志")
        )
        assert candidate.status == "candidate" and candidate.type == "experience"
        assert candidate.sync_status == "not_synced"
        assert state["sources"] == {}
        await service.publish_knowledge(
            candidate.id, KnowledgePublishWrite(space_id=space_id), LibraryPrincipal()
        )
        source = knowledge_source_path(candidate.id)
        assert "astra_knowledge_id:" in state["sources"][source]
        hits = await service.mcp_search(
            LibrarySearchInput(query="流水线环境初始化超时"), runtime_context=runtime_context
        )
        resource = await service.mcp_get(hits[0].resource_id, runtime_context=runtime_context)
        assert "检查初始化日志" in resource.content_md
        feedback = await service.mcp_feedback(
            LibraryFeedbackInput(resource_id=resource.resource_id, feedback_type="helpful")
        )
        assert feedback.resource_id == resource.resource_id
        events = (
            await session.scalars(
                select(KnowledgeUsage).where(KnowledgeUsage.resource_id == resource.resource_id)
            )
        ).all()
        assert [event.action for event in events] == ["search_hit", "read", "feedback"]
        assert all(
            event.employee_id == "E-wiki"
            and event.task_id == "T-wiki"
            and event.session_id == "ses_wiki"
            for event in events
            if event.action in {"search_hit", "read"}
        )
        summary = await service.usage_summary()
        assert summary.total_references == 1 and summary.used_resources == 1
        assert summary.feedback_count == 1
        await service.update_knowledge(
            candidate.id, KnowledgePatch(content_md="更新后的步骤"), LibraryPrincipal()
        )
        assert (
            "更新后的步骤"
            in (await service.mcp_get(resource.resource_id, runtime_context=runtime_context)).content_md
        )
        await service.archive_knowledge(candidate.id, LibraryPrincipal())
        assert source not in state["sources"]
        assert (
            await service.mcp_search(
                LibrarySearchInput(query="初始化"), runtime_context=runtime_context
            )
            == []
        )
        with pytest.raises(LlmWikiError, match="不存在"):
            await service.mcp_get(resource.resource_id, runtime_context=runtime_context)
        await service.delete_knowledge(candidate.id, LibraryPrincipal())
        assert await session.get(KnowledgeItem, candidate.id) is None
        assert await session.scalar(select(func.count(KnowledgeFeedback.id))) == 1
        assert (await service.mcp_related(resource.resource_id))["status"] == "unsupported"


async def test_batch_publish_syncs_every_record(wiki_env):
    settings, sessions, state, space_id = wiki_env
    async with sessions() as session:
        service = KnowledgeService(session)
        ids = []
        for title in ("first", "second"):
            item = await service.create_knowledge(
                KnowledgeCreate(space_id=space_id, title=title, content_md=title), LibraryPrincipal()
            )
            ids.append(item.id)
        await service.batch_publish(ids, space_id, "verified", LibraryPrincipal())
        for knowledge_id in ids:
            item = await session.get(KnowledgeItem, knowledge_id)
            assert item.status == "published" and item.sync_status == "synced"
            assert knowledge_source_path(knowledge_id) in state["sources"]
        await service.batch_archive(ids, LibraryPrincipal())
        assert state["sources"] == {}


async def test_scope_change_withdraws_descendants(wiki_env):
    settings, sessions, state, space_id = wiki_env
    async with sessions() as session:
        service = KnowledgeService(session)
        child = await service.create_space(
            KnowledgeSpaceCreate(name="Child", parent_id=space_id), LibraryPrincipal()
        )
    knowledge_id = await publish(sessions, child.id)
    expected = knowledge_source_path(knowledge_id)
    assert expected in state["sources"]
    async with sessions() as session:
        service = KnowledgeService(session)
        await service.update_space(
            space_id, KnowledgeSpacePatch(access_scope="admin"), LibraryPrincipal()
        )
    assert expected not in state["sources"]


async def test_disabled_service_and_all_feedback_types(wiki_env):
    settings, sessions, state, _ = wiki_env
    settings.llm_wiki_enabled = False
    async with sessions() as session:
        service = KnowledgeService(session)
        with pytest.raises(LlmWikiError, match="知识服务暂不可用：未启用"):
            await service.mcp_search(
                LibrarySearchInput(query="anything"), runtime_context=state["runtime_context"]
            )
        for kind in ("helpful", "incorrect", "outdated", "missing", "mismatch"):
            result = await service.mcp_feedback(
                LibraryFeedbackInput(resource_id="wiki/topic.md", feedback_type=kind)
            )
            assert result.feedback_type == kind
        assert await session.scalar(select(func.count(KnowledgeFeedback.id))) == 5


async def test_mcp_expected_errors_are_not_masked_by_sdk(wiki_env, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from app.mcp_server import mcp

    settings, sessions, state, _ = wiki_env
    settings.llm_wiki_enabled = False
    monkeypatch.setattr("app.mcp_server.SessionLocal", sessions)
    runtime_context = state["runtime_context"]
    with pytest.raises(ToolError, match="知识服务暂不可用") as error:
        await mcp.call_tool(
            "library_search", {"query": "timeout", "runtime_context": runtime_context}
        )
    assert type(error.value) is ToolError
    with pytest.raises(ToolError, match="limit") as error:
        await mcp.call_tool(
            "library_search", {"query": "timeout", "limit": 0, "runtime_context": runtime_context}
        )
    assert type(error.value) is ToolError


@pytest.mark.parametrize("path", ["../secret.md", "/wiki/a.md", "raw/sources/a.md", "wiki/../secret.md"])
async def test_read_rejects_non_wiki_paths(wiki_env, path):
    settings, _, state, _ = wiki_env
    with pytest.raises(LlmWikiError, match="resource_id"):
        await LlmWikiClient(settings).read(path)
    assert not state["calls"]


def test_migration_backfills_usage_and_published_sources(tmp_path):
    backend = Path(__file__).resolve().parents[2]
    database = tmp_path / "migration.db"
    env = {**os.environ, "ASTRA_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    migrate("upgrade", "0018_merge_runtime_category")
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO knowledge_items "
            "(id,title,summary,content_md,status,tags,experience_data,created_by,updated_by,"
            "created_at,updated_at) VALUES "
            "('KNOW_old','old','','body','published','[]','{}','admin','admin',"
            "'2026-09-08 00:00:00','2026-09-08 00:00:00')"
        )
        conn.execute(
            "INSERT INTO knowledge_usage (knowledge_id,tool_name,action,result,created_at) "
            "VALUES ('KNOW_old','library_get','reference','{}','2026-09-08 00:00:00')"
        )
        conn.execute(
            "INSERT INTO knowledge_usage (tool_name,action,result,created_at) "
            "VALUES ('library_search','search','{\"knowledge_ids\":[\"KNOW_old\"]}',"
            "'2026-09-08 00:00:00')"
        )
    migrate("upgrade", "0019_library_llm_wiki")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT resource_id,action FROM knowledge_usage ORDER BY id").fetchall() == [
            ("astra:KNOW_old", "read"),
            ("astra:KNOW_old", "search_hit"),
        ]
        assert conn.execute("SELECT knowledge_id,status FROM knowledge_sync").fetchone() == (
            "KNOW_old",
            "pending",
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate("upgrade", "0029_knowledge_sync_state")
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT sync_status, source_path FROM knowledge_items WHERE id='KNOW_old'"
        ).fetchone() == ("pending", "raw/sources/astra/knowledge/KNOW_old.md")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "knowledge_sync" not in tables
    migrate("downgrade", "0028_usage_breakdowns_and_task_completion")
    migrate("upgrade", "0029_knowledge_sync_state")


def test_llm_wiki_url_allows_remote_and_https():
    from pydantic import ValidationError

    from app.config.settings import Settings

    # Valid URLs: local, remote IP, domain, https, custom port
    for valid_url in (
        "http://127.0.0.1:19828/api/v1",
        "http://192.168.1.100:19828/api/v1",
        "https://wiki.example.com/api/v1",
        "https://wiki.company.internal:8443/api/v1",
        "http://remote-host:8000/api/v1/",
    ):
        settings = Settings(
            llm_wiki_api_base_url=valid_url,
            _env_file=None,
        )
        assert settings.llm_wiki_api_base_url == valid_url.rstrip("/")

    # Invalid URLs: invalid scheme, missing host, query params, fragments, auth, bad path
    for invalid_url in (
        "ftp://wiki.example.com/api/v1",
        "http:///api/v1",
        "https://wiki.example.com/api/v1?token=123",
        "https://wiki.example.com/api/v1#section",
        "https://user:pass@wiki.example.com/api/v1",
        "https://wiki.example.com/api/v2",
        "https://wiki.example.com/",
    ):
        with pytest.raises(ValidationError, match="llm_wiki_api_base_url"):
            Settings(
                llm_wiki_api_base_url=invalid_url,
                _env_file=None,
            )