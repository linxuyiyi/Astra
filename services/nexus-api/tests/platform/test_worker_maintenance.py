"""Worker 5 态无锁维护执行器、离线快速唤醒、就绪投影与列表/配置 API 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import update

import app.gateway.models  # noqa: F401 - 注册 GatewayRoute/GatewayKey 表
from app.config.settings import get_settings
from app.database import SessionLocal
from app.models import WorkerMaintenanceOperation
from app.runtime.maintenance import (
    submit_operation,
)
from app.runtime.remote_provisioning import worker_tools_wheel

ACTOR_HEADERS = {"X-Owner-Id": "admin-test"}


@pytest.fixture(autouse=True)
def maintenance_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gateway_master_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "mcp_transport", "streamable-http")
    monkeypatch.setattr(settings, "gateway_openai_base", "http://10.0.0.8:8110")
    monkeypatch.setattr(settings, "mcp_resource_server_url", "http://10.0.0.8:8201/mcp")


async def make_operation(worker_id: str, action: str = "deploy") -> WorkerMaintenanceOperation:
    async with SessionLocal() as db:
        return await submit_operation(db, worker_id, action, requested_by="test")


async def read_operation(operation_id: str) -> WorkerMaintenanceOperation | None:
    async with SessionLocal() as db:
        operation = await db.get(WorkerMaintenanceOperation, operation_id)
        if operation is not None:
            db.expunge(operation)
        return operation


async def expire_lease(operation_id: str) -> None:
    async with SessionLocal() as db:
        await db.execute(
            update(WorkerMaintenanceOperation)
            .where(WorkerMaintenanceOperation.id == operation_id)
            .values(lease_until=datetime.now(UTC) - timedelta(seconds=1))
        )
        await db.commit()


# ---------------------------------------------------------------------------
# 1. 5 态状态机、无锁超时回收、取消、重试与 SSE 恢复
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. restart 在线快路径 vs 离线轻量 SSH ctl.sh start 快速唤醒 & update 路由
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. 部署幂等复用凭据、密钥轮换保护与 Wheel 可复现性
# ---------------------------------------------------------------------------


def test_worker_tools_wheel_is_reproducible_and_named_for_nexus():
    name1, bytes1 = worker_tools_wheel()
    name2, bytes2 = worker_tools_wheel()
    assert name1 == name2
    assert name1.startswith("nexus_worker_tools-")
    assert bytes1 == bytes2


# ---------------------------------------------------------------------------
# 4. 列表内嵌 worker_health、PATCH enabled、PUT environment 与 Dispatcher 跳过异常节点
# ---------------------------------------------------------------------------
