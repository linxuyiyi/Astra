import asyncio
import os
import re
import tempfile
from pathlib import Path

import pytest

SESSION_REDIS_PREFIX = f"astra:worker-gateway:test:{os.getpid()}"
SESSION_REVOKED_MEMBER_PREFIX = f"astra-test-revoked:{os.getpid()}:"
_OLD_REVOKED_MEMBER = re.compile(r"rev-[0-9a-f]{8}-[abc]")

TEMP_ROOT = Path(tempfile.gettempdir())
TEST_DB = TEMP_ROOT / f"astra-test-{os.getpid()}.db"
TEST_WORKSPACES = TEMP_ROOT / f"astra-workspaces-{os.getpid()}"
os.environ["ASTRA_DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB.as_posix()}"
os.environ["ASTRA_SEED_DEMO"] = "false"
os.environ["NEXUS_WORKER_GATEWAY_ADMIN_KEY"] = "wgk_test"
os.environ["ASTRA_LLM_WIKI_ENABLED"] = "false"
os.environ["ASTRA_WORKSPACE_ROOT"] = str(TEST_WORKSPACES)


@pytest.fixture(autouse=True)
def isolate_proxy_secret_cache():
    from app.api.workspace_routes import clear_proxy_secret_cache

    clear_proxy_secret_cache()
    yield
    clear_proxy_secret_cache()


@pytest.fixture(autouse=True)
async def prepare_process_schema():
    """The process database URL is a fresh file. Create its schema before any lookup.

    Standalone modules such as ``test_waiting_reason`` use their own engine, but
    ``environment_uses_worker`` still reads ``worker_channel`` through SessionLocal.
    """

    import app.gateway.models  # noqa: F401 - register gateway tables
    from app.database import init_database

    await init_database()


@pytest.fixture(autouse=True)
async def clear_worker_maintenance(prepare_process_schema):
    """Keep persisted maintenance from one test from being claimed by another."""

    from sqlalchemy import delete

    from app.database import SessionLocal
    from app.models import WorkerMaintenanceEvent, WorkerMaintenanceOperation

    async with SessionLocal() as session:
        await session.execute(delete(WorkerMaintenanceEvent))
        await session.execute(delete(WorkerMaintenanceOperation))
        await session.commit()
    yield


@pytest.fixture(autouse=True)
def keep_redis_url_stable():
    """全量跑时，前面的用例清掉缓存或改掉环境变量后，把 Redis URL 还回来。"""

    expected = os.environ.get("ASTRA_REDIS_URL")
    yield
    if expected is not None:
        os.environ["ASTRA_REDIS_URL"] = expected
    from app.config.settings import get_settings

    if get_settings.cache_info().currsize and expected and get_settings().redis_url != expected:
        get_settings.cache_clear()
    import app.worker_gateway.service as gateway_service

    gateway = gateway_service._gateway
    if gateway is not None and expected and gateway.settings.redis_url != expected:
        gateway_service._gateway = None


def pytest_sessionfinish(session, exitstatus):
    from app.database import engine

    asyncio.run(engine.dispose())
    TEST_DB.unlink(missing_ok=True)
    url = os.environ.get("ASTRA_TEST_REDIS_URL")
    if not url:
        return
    from redis import Redis

    client = Redis.from_url(url)
    try:
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=f"{SESSION_REDIS_PREFIX}:*", count=500)
            if keys:
                client.delete(*keys)
            if cursor == 0:
                break
        members = client.smembers("nexus:gateway:revoked_keys")
        stale = []
        for member in members:
            text = member.decode() if isinstance(member, bytes) else str(member)
            if text.startswith(SESSION_REVOKED_MEMBER_PREFIX) or _OLD_REVOKED_MEMBER.fullmatch(text):
                stale.append(member)
        if stale:
            client.srem("nexus:gateway:revoked_keys", *stale)
    finally:
        client.close()
