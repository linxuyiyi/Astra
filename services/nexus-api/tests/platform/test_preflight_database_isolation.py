"""数据库隔离自检回归。

背景：迁移到 0031 后，``check_database`` 只注册了 ``app.assistant.models``，
导致 ``Base.metadata`` 缺少 Gateway 表，8 张 ``gateway_*`` 表被误报为“非
Astra 表”。这里直接校验注册与判定逻辑，不连接真实数据库。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PREFLIGHT_SCRIPT = ROOT / "ops" / "dev" / "preflight.py"

GATEWAY_TABLES = {
    "t_nexus_gateway_pools",
    "t_nexus_gateway_nodes",
    "t_nexus_gateway_routes",
    "t_nexus_gateway_keys",
    "t_nexus_gateway_health_checks",
    "t_nexus_gateway_requests",
    "t_nexus_gateway_audit_events",
}


def _load_preflight_module():
    spec = importlib.util.spec_from_file_location("preflight_under_test", PREFLIGHT_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight_module()


def test_gateway_tables_are_registered_as_astra_tables():
    astra_tables = preflight.astra_table_names()
    assert GATEWAY_TABLES <= astra_tables
    assert not preflight.foreign_table_names(astra_tables | {"alembic_version"})


def test_real_external_tables_are_still_reported():
    tables = preflight.astra_table_names() | {"alembic_version", "foreign_reports"}
    assert preflight.foreign_table_names(tables) == ["foreign_reports"]