"""统一服务脚本的 Gateway 集成校验。

不启动真实进程：只校验 ``ops/dev/astra_service.py`` 组装出的命令、就绪等待和
正式模式校验，确保 API 中转数据面以独立进程运行并直接加载保存配置。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
SERVICE_SCRIPT = ROOT / "ops" / "dev" / "astra_service.py"


def _load_service_module():
    spec = importlib.util.spec_from_file_location("astra_service_under_test", SERVICE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


service = _load_service_module()
GATEWAY_MODULE = "app.gateway.server:app"


def _settings(*, gateway_enabled: bool = True, **overrides):
    base = {
        "gateway_enabled": gateway_enabled,
        "gateway_master_key": None,
        "redis_url": "redis://127.0.0.1:6379/0",
        "mcp_transport": "stdio",
        "executor_type": "opencode_server",
        "database_url": "mysql+asyncmy://nexus@127.0.0.1:3306/nexus",
        "database_backend": "mysql",
        "resolved_runtime_dir": Path("/tmp/astra-run"),
        "resolved_log_dir": Path("/tmp/astra-logs"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _local_command(executable, args):
    return [executable, *args]


def test_gateway_runs_as_standalone_data_plane():
    command = service.gateway_command("pro")
    assert command[command.index("-m") + 1] == "uvicorn"
    assert GATEWAY_MODULE in command
    assert str(service.GATEWAY_PORT) in command
    # API 由 nexus_api.service 单进程提供，网关数据面不进入主服务。
    assert "nexus_api.service:app" not in command


def test_core_services_adds_gateway_only_when_enabled():
    enabled = service.core_services("pro", _settings(gateway_enabled=True), "npm", _local_command)
    disabled = service.core_services(
        "pro", _settings(gateway_enabled=False), "npm", _local_command
    )
    assert "gateway" in [name for name, *_ in enabled]
    assert "gateway" not in [name for name, *_ in disabled]
    gateway_entry = next(entry for entry in enabled if entry[0] == "gateway")
    assert GATEWAY_MODULE in gateway_entry[1]
    # 生产模式由 Nexus 服务直接托管 dist，不再单独启动前端进程。
    assert "frontend" not in [name for name, *_ in enabled]
    dev = service.core_services("dev", _settings(gateway_enabled=True), "npm", _local_command)
    assert dev[-1][0] == "frontend"


def test_start_core_waits_for_gateway_health(monkeypatch):
    processes: dict[str, dict[str, object]] = {}
    ready_urls: list[str] = []

    def fake_spawn(settings, name, command, cwd, *, env=None):
        processes[name] = {"pid": 1, "log": f"/tmp/{name}.log", "command": command, "cwd": str(cwd)}
        return processes[name]

    monkeypatch.setattr(service, "spawn", fake_spawn)
    monkeypatch.setattr(service, "wait_process", lambda name, info: None)
    monkeypatch.setattr(service, "wait_worker", lambda name, info: None)
    def record_ready(name, url, timeout=45, **kwargs):
        ready_urls.append(url)

    monkeypatch.setattr(service, "wait_http", record_ready)

    service.start_core("dev", _settings(gateway_enabled=True), "npm", _local_command, processes)

    assert "gateway" in processes
    assert "task-mcp" in processes
    assert "schedule-mcp" in processes
    assert f"http://127.0.0.1:{service.GATEWAY_PORT}/healthz" in ready_urls
    assert f"http://127.0.0.1:{service.TASK_MCP_PORT}/health" in ready_urls
    assert f"http://127.0.0.1:{service.SCHEDULE_MCP_PORT}/health" in ready_urls


def test_gateway_startup_has_no_publication_workflow():
    source = (
        ROOT / "services" / "nexus-api" / "app" / "gateway" / "server.py"
    ).read_text(encoding="utf-8")
    assert "publish" not in source.lower()


@pytest.mark.parametrize(
    "overrides",
    [
        {"gateway_master_key": None},
        {"gateway_master_key": "fernet-key", "redis_url": ""},
    ],
)
def test_production_gateway_validation_rejects_missing_dependencies(overrides):
    with pytest.raises(RuntimeError):
        service.validate_gateway_production(_settings(gateway_enabled=True, **overrides))


def test_production_gateway_validation_accepts_config_without_management_credentials():
    service.validate_gateway_production(
        _settings(
            gateway_enabled=True,
            gateway_master_key="fernet-key",
        )
    )


def test_production_gateway_validation_skips_when_disabled():
    service.validate_gateway_production(
        _settings(gateway_enabled=False, gateway_master_key=None)
    )


def _workspace_ui_tree(
    tmp_path: Path,
    *,
    installed: list[str],
    upstream_tag: str | None = "v1.18.35",
    upstream_raw: str | None = None,
):
    frontend = tmp_path / "frontend"
    (frontend / "node_modules").mkdir(parents=True)
    bundles = frontend / "public" / "opencode-workspace"
    bundles.mkdir(parents=True)
    # 历史遗留的根产物存在，但版本化产物才是 Runtime 实际引用的入口。
    (bundles / "index.html").write_text("legacy", encoding="utf-8")
    for version in installed:
        (bundles / version).mkdir(parents=True)
        (bundles / version / "index.html").write_text("ok", encoding="utf-8")
    upstream = tmp_path / "web" / "workspace-ui"
    upstream.mkdir(parents=True)
    # upstream_tag=None 表示 UPSTREAM.json 缺失，upstream_raw 用于写入损坏内容。
    if upstream_raw is not None:
        (upstream / "UPSTREAM.json").write_text(upstream_raw, encoding="utf-8")
    elif upstream_tag is not None:
        (upstream / "UPSTREAM.json").write_text(
            json.dumps({"tag": upstream_tag}), encoding="utf-8"
        )
    env_dir = tmp_path / "ops" / "env"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text("", encoding="utf-8")
    return frontend


def test_running_opencode_version_requires_its_own_bundle(tmp_path, monkeypatch):
    frontend = _workspace_ui_tree(tmp_path, upstream_tag="v1.18.35", installed=["1.18.35"])
    monkeypatch.setattr(service, "FRONTEND", frontend)
    with pytest.raises(RuntimeError, match="9.9.9"):
        service.ensure_workspace_ui_bundle("v9.9.9")
    with pytest.raises(RuntimeError, match="1.2.27"):
        service.ensure_workspace_ui_bundle("1.2.27")
    service.ensure_workspace_ui_bundle("1.18.35")


def test_validate_start_accepts_the_single_upstream_bundle(tmp_path, monkeypatch):
    frontend = _workspace_ui_tree(tmp_path, upstream_tag="v1.18.35", installed=["1.18.35"])
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "FRONTEND", frontend)

    service.validate_start("dev", _settings())


def test_validate_start_rejects_missing_upstream_bundle(tmp_path, monkeypatch):
    frontend = _workspace_ui_tree(tmp_path, upstream_tag="v1.18.35", installed=["1.2.27"])
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "FRONTEND", frontend)

    with pytest.raises(RuntimeError) as excinfo:
        service.validate_start("dev", _settings())

    message = str(excinfo.value)
    assert "1.18.35" in message
    assert "index.html" in message
    assert "restore" in message.lower()


@pytest.mark.parametrize("upstream_raw", [None, "{ not valid json"])
def test_validate_start_rejects_invalid_upstream_metadata(
    tmp_path, monkeypatch, upstream_raw
):
    # 上游元数据缺失或损坏时必须显式失败，不能静默跳过固定上游 UI 校验。
    if upstream_raw is None:
        frontend = _workspace_ui_tree(tmp_path, upstream_tag=None, installed=["1.18.35"])
    else:
        frontend = _workspace_ui_tree(tmp_path, upstream_raw=upstream_raw, installed=["1.18.35"])
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "FRONTEND", frontend)

    with pytest.raises(RuntimeError) as excinfo:
        service.validate_start("dev", _settings())

    assert "UPSTREAM.json" in str(excinfo.value)

def test_validate_start_worker_mode_skips_local_opencode_bundle(tmp_path, monkeypatch):
    # 公司部署只用远程 Worker：没有本地 workspace-ui 产物也必须能启动。
    frontend = _workspace_ui_tree(tmp_path, upstream_tag="v1.18.35", installed=[])
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "FRONTEND", frontend)

    service.validate_start("dev", _settings(executor_type="worker"))
    service.validate_start(
        "pro", _settings(executor_type="worker", gateway_enabled=False)
    )


def test_production_rejects_cli_fallback_executor(tmp_path, monkeypatch):
    frontend = _workspace_ui_tree(tmp_path, upstream_tag="v1.18.35", installed=["1.18.35"])
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service, "FRONTEND", frontend)

    with pytest.raises(RuntimeError, match="opencode_server"):
        service.validate_start(
            "pro", _settings(executor_type="opencode_cli_fallback", gateway_enabled=False)
        )


def test_shared_healthy_serve_rejects_old_vendor_config_before_reuse(monkeypatch):
    from app.runtime.opencode_server import OpenCodeServerManager

    class OldServe(OpenCodeServerManager):
        async def health(self):
            return SimpleNamespace(healthy=True)

        async def validate_gateway_configuration(self):
            raise RuntimeError("已有 Serve 未使用统一网关配置")

    def unexpected_spawn(*args, **kwargs):
        pytest.fail("不得接管或重启未通过配置校验的已有进程")

    monkeypatch.setattr(service, "spawn", unexpected_spawn)
    with pytest.raises(RuntimeError, match="统一网关"):
        service.start_opencode(_settings(opencode_server_url=None, opencode_binary="opencode"), OldServe, {})
