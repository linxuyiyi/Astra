import json

import httpx
import pytest
from sqlalchemy import make_url, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap import bootstrap_employees
from app.config.settings import BACKEND_ROOT, Settings
from app.models import AgentWorker, Base, OpenCodeExecutionEnvironment
from app.sources.nr_test_system import NrTestSystemSource


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def test_bootstrap_employee_from_v03_yaml(tmp_path, session):
    (tmp_path / "employees.yaml").write_text(
        """employees:
  - id: astra-config
    name: Astra Config
    role: 测试平台员工
    task_types: [ISSUE, REQUIREMENT]
    category_ids: [testing-platform]
    policy:
      auto_claim: true
      auto_execute: false
      max_auto_retries: 2
    execution_environment:
      mode: LOCAL
      workspace_root: ./workspaces/astra-config
""",
        encoding="utf-8",
    )

    await bootstrap_employees(session, tmp_path)
    employee = await session.get(AgentWorker, "astra-config")

    assert employee is not None
    assert employee.status == "IDLE"
    assert employee.task_types == ["ISSUE", "REQUIREMENT"]
    assert employee.category_ids == ["testing-platform"]
    assert employee.auto_execute is False
    environment = await session.scalar(
        select(OpenCodeExecutionEnvironment).where(OpenCodeExecutionEnvironment.employee_id == employee.id)
    )
    assert environment.workspace_root == "./workspaces/astra-config"


async def test_nr_test_system_adapter_maps_v03_task_and_backfills():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "EXT-42",
                            "task_type": "ISSUE",
                            "category_id": "testing-platform",
                            "title": "修复登录接口异常",
                            "description": "登录接口偶发返回 500",
                            "acceptance_criteria": ["异常已修复", "原能力正常"],
                            "priority": "P1",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"ok": True})

    settings = Settings(
        nr_test_system_endpoint="http://nr-test.local",
        nr_test_system_token="test-token",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.nr_test_system_endpoint
    ) as client:
        source = NrTestSystemSource(settings, client)
        tasks = await source.discover_tasks()
        await source.update_task("EXT-42", {"status": "COMPLETED"})

    assert tasks[0].external_task_id == "EXT-42"
    assert tasks[0].task_type == "ISSUE"
    assert tasks[0].category_id == "testing-platform"
    assert tasks[0].priority == "HIGH"
    assert tasks[0].acceptance_criteria == ["异常已修复", "原能力正常"]
    assert requests[0].url.params["status"] == "READY"
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert requests[1].url.path == "/api/astra/tasks/EXT-42/result"
    assert json.loads(requests[1].content) == {"status": "COMPLETED"}


async def test_nr_test_system_skips_http_without_token():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP {request.method} {request.url}")

    settings = Settings(
        nr_test_system_endpoint="http://127.0.0.1:8000",
        nr_test_system_token="  ",
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=settings.nr_test_system_endpoint
    ) as client:
        source = NrTestSystemSource(settings, client)
        assert await source.discover_tasks() == []
        await source.update_task("EXT-1", {"status": "COMPLETED"})
        assert await source.heartbeat() is False
        with pytest.raises(RuntimeError, match="token is not configured"):
            await source.get_task("EXT-1")


def test_scout_returns_empty_without_token(monkeypatch):
    from app.runtime import celery_app

    monkeypatch.setattr(celery_app.settings, "nr_test_system_token", None)
    assert celery_app.scout() == []


def test_mysql_settings_build_encoded_async_url_without_exposing_password():
    settings = Settings(
        database_url=None,
        database_backend="mysql",
        database_host="mysql.company.local",
        database_port=3307,
        database_name="astra_company",
        database_username="astra user",
        database_password="p@ss:/ word",
        database_charset="utf8mb4",
    )

    url = make_url(settings.sqlalchemy_database_url)

    assert url.drivername == "mysql+asyncmy"
    assert url.host == "mysql.company.local"
    assert url.port == 3307
    assert url.database == "astra_company"
    assert url.username == "astra user"
    assert url.password == "p@ss:/ word"
    assert url.query["charset"] == "utf8mb4"
    assert "p@ss" not in settings.database_description


def test_redis_description_hides_credentials():
    settings = Settings(redis_url="redis://astra:secret@redis.company.local:6380/2")

    assert settings.redis_description == "redis://redis.company.local:6380/2"
    assert "secret" not in settings.redis_description


def test_legacy_relative_sqlite_url_keeps_backend_data_location():
    settings = Settings(database_url="sqlite+aiosqlite:///./data/astra.db")

    expected = (BACKEND_ROOT / "data" / "astra.db").resolve().as_posix()
    assert settings.sqlalchemy_database_url == f"sqlite+aiosqlite:///{expected}"
