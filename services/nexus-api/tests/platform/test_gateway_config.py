"""保存配置的真实 SQLite 加载、刷新、重启与事务一致性。"""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.gateway.config import ConfigLoader, ConfigRefresher, GatewayConfig, GatewayConfigState
from app.gateway.models import GatewayKey, GatewayNode, GatewayPool, GatewayRoute, utcnow
from app.gateway.proxy import GatewayProxy
from app.gateway.runtime import InMemoryRuntimeStore
from app.gateway.security import CredentialCipher, gateway_key_digest
from app.gateway.server import GatewayDataPlane

POOL_ID = "gp_saved"
NODE_ID = "gn_saved"
ROUTE_ID = "gr_saved"
MODEL = "saved-model"
KEY = "agw_saved-key"
REVOKED_KEY = "agw_revoked-key"
CONFIG_TABLES = [model.__table__ for model in (GatewayPool, GatewayNode, GatewayRoute, GatewayKey)]


@pytest.fixture
async def config_database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gateway-config.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: GatewayPool.metadata.create_all(sync_connection, tables=CONFIG_TABLES)
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


async def save_configuration(factory):
    async with factory.begin() as session:
        session.add(GatewayPool(id=POOL_ID, name="保存的资源池"))
        session.add(
            GatewayNode(
                id=NODE_ID,
                name="保存的节点",
                pool_id=POOL_ID,
                base_url="https://saved.example.com",
                models=[MODEL],
                config_state="ENABLED",
                scheduling_state="DRAINING",
                max_concurrency=4,
            )
        )
        session.add(GatewayRoute(id=ROUTE_ID, model=MODEL, pool_id=POOL_ID))
        session.add(
            GatewayKey(
                id="gk_saved",
                name="保存的 Key",
                key_prefix=KEY[:12],
                key_digest=gateway_key_digest(KEY),
                allowed_models=[MODEL],
                daily_token_limit=500,
                expires_at=utcnow() + timedelta(days=1),
            )
        )
        session.add(
            GatewayKey(
                id="gk_revoked",
                name="已吊销 Key",
                key_prefix=REVOKED_KEY[:12],
                key_digest=gateway_key_digest(REVOKED_KEY),
                enabled=False,
                revoked_at=utcnow(),
            )
        )


async def test_loader_uses_saved_tables_without_any_publication(config_database):
    _engine, factory = config_database
    loader = ConfigLoader(factory)
    assert await loader.load_current() == GatewayConfig.empty()
    await save_configuration(factory)

    config = await loader.load_current()
    assert config.pools[POOL_ID].name == "保存的资源池"
    assert config.nodes[NODE_ID].base_url == "https://saved.example.com"
    assert config.nodes[NODE_ID].max_concurrency == 4
    assert config.routes[MODEL].pool_id == POOL_ID
    assert config.keys[gateway_key_digest(KEY)].allowed_models == (MODEL,)
    assert config.keys[gateway_key_digest(KEY)].daily_token_limit == 500
    assert config.keys[gateway_key_digest(KEY)].expires_at > utcnow()
    assert config.keys[gateway_key_digest(REVOKED_KEY)].enabled is False
    assert config.keys[gateway_key_digest(REVOKED_KEY)].revoked_at is not None

    async with factory.begin() as session:
        node = await session.get(GatewayNode, NODE_ID)
        node.base_url = "https://changed.example.com"
    changed = await loader.load_current()
    assert changed.nodes[NODE_ID].base_url == "https://changed.example.com"
    assert config.nodes[NODE_ID].base_url == "https://saved.example.com"


async def test_refresh_failure_keeps_entire_old_view_then_recovers(config_database):
    engine, factory = config_database
    await save_configuration(factory)
    loader = ConfigLoader(factory)
    original = await loader.load_current()
    state = GatewayConfigState(original)
    refresher = ConfigRefresher(state=state, loader=loader, interval=1)

    async with engine.begin() as connection:
        await connection.execute(
            text("UPDATE t_nexus_gateway_pools SET name = '改名池' WHERE id = :id"), {"id": POOL_ID}
        )
        await connection.execute(text("ALTER TABLE t_nexus_gateway_nodes RENAME TO unavailable_nodes"))
    await refresher.tick()
    assert state.config is original
    assert state.config.pools[POOL_ID].name == "保存的资源池"

    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE unavailable_nodes RENAME TO t_nexus_gateway_nodes"))
    await refresher.tick()
    assert state.config.pools[POOL_ID].name == "改名池"
    assert state.config.nodes[NODE_ID] == original.nodes[NODE_ID]
    assert original.pools[POOL_ID].name == "保存的资源池"


async def test_identical_configuration_preserves_view_object(config_database):
    _engine, factory = config_database
    await save_configuration(factory)
    original = await ConfigLoader(factory).load_current()
    state = GatewayConfigState(original)
    refresher = ConfigRefresher(state=state, loader=ConfigLoader(factory), interval=1)
    await refresher.tick()
    assert state.config is original


def build_plane(factory, instance_id):
    return GatewayDataPlane(
        config_state=GatewayConfigState(),
        config_loader=ConfigLoader(factory),
        runtime=InMemoryRuntimeStore(),
        proxy=GatewayProxy(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        cipher=CredentialCipher(None),
        session_factory=factory,
        instance_id=instance_id,
        config_poll_interval=3600,
        heartbeat_interval=3600,
    )


async def test_startup_restart_and_two_instances_adopt_same_saved_configuration(config_database):
    _engine, factory = config_database
    await save_configuration(factory)
    first = build_plane(factory, "first")
    second = build_plane(factory, "second")
    restarted = build_plane(factory, "restarted")
    started = []
    try:
        for plane in (first, second):
            await plane.startup()
            started.append(plane)
        assert first.config_state.config == second.config_state.config
        assert first.config_state.config.nodes[NODE_ID].base_url == "https://saved.example.com"
        old_view = first.config_state.config

        async with factory.begin() as session:
            node = await session.get(GatewayNode, NODE_ID)
            node.base_url = "https://restarted.example.com"
        refreshers = [
            ConfigRefresher(state=plane.config_state, loader=ConfigLoader(factory), interval=1)
            for plane in (first, second)
        ]
        await asyncio.gather(*(refresher.tick() for refresher in refreshers))
        assert first.config_state.config == second.config_state.config
        assert first.config_state.config.nodes[NODE_ID].base_url == "https://restarted.example.com"
        assert old_view.nodes[NODE_ID].base_url == "https://saved.example.com"

        await first.shutdown()
        started.remove(first)
        await restarted.startup()
        started.append(restarted)
        assert restarted.config_state.config == second.config_state.config
    finally:
        for plane in reversed(started):
            await plane.shutdown()


async def test_loader_keeps_one_database_view_during_concurrent_configuration_save(config_database):
    engine, factory = config_database
    await save_configuration(factory)
    wrote_between_reads = False

    class InterleavedSession(AsyncSession):
        async def scalars(self, statement, *args, **kwargs):
            nonlocal wrote_between_reads
            result = await super().scalars(statement, *args, **kwargs)
            if statement.column_descriptions[0]["entity"] is GatewayPool and not wrote_between_reads:
                async with factory.begin() as writer:
                    writer.add(GatewayPool(id="gp_replacement", name="新资源池"))
                    node = await writer.get(GatewayNode, NODE_ID)
                    route = await writer.get(GatewayRoute, ROUTE_ID)
                    node.pool_id = "gp_replacement"
                    route.pool_id = "gp_replacement"
                wrote_between_reads = True
            return result

    interleaved_factory = async_sessionmaker(engine, class_=InterleavedSession, expire_on_commit=False)
    loaded_during_save = await ConfigLoader(interleaved_factory).load_current()
    assert wrote_between_reads
    assert set(loaded_during_save.pools) == {POOL_ID}
    assert loaded_during_save.nodes[NODE_ID].pool_id == POOL_ID
    assert loaded_during_save.routes[MODEL].pool_id == POOL_ID

    loaded_after_save = await ConfigLoader(factory).load_current()
    assert "gp_replacement" in loaded_after_save.pools
    assert loaded_after_save.nodes[NODE_ID].pool_id == "gp_replacement"
    assert loaded_after_save.routes[MODEL].pool_id == "gp_replacement"
