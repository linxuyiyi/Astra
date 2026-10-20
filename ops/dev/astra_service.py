#!/usr/bin/env python3
"""Astra unified service lifecycle entry."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _load_procman():
    path = Path(__file__).resolve().parent / "procman.py"
    spec = importlib.util.spec_from_file_location("astra_procman", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


procman = _load_procman()

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "services" / "nexus-api"
FRONTEND = ROOT / "services" / "nexus-api" / "web"

BACKEND_PORT = int(os.getenv("NEXUS_API_PORT", "18120"))
FRONTEND_HOST = os.getenv("ASTRA_SERVICE_FRONTEND_HOST", "0.0.0.0")
FRONTEND_PORT = int(os.getenv("ASTRA_SERVICE_FRONTEND_PORT", "15174"))
# API 中转数据面是独立 ASGI 进程（app.gateway.server:app）。默认只监听 loopback，
# 由同机 nginx 反代 /v1；模型长连接绝不进入主 Backend。
GATEWAY_HOST = os.getenv("ASTRA_SERVICE_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("ASTRA_SERVICE_GATEWAY_PORT", "18110"))
TASK_MCP_PORT = int(os.getenv("NEXUS_TASK_MCP_PORT", "18202"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Astra service lifecycle manager")
    parser.add_argument("mode", choices=("dev", "pro"), help="dev=development, pro=production")
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=("start", "stop", "restart", "status"),
    )
    return parser.parse_args()


def load_astra(mode: str) -> tuple[Any, Any, Any]:
    os.environ["ASTRA_ENVIRONMENT"] = "development" if mode == "dev" else "production"
    if mode == "pro":
        os.environ["ASTRA_SEED_DEMO"] = "false"

    sys.path.insert(0, str(BACKEND))
    from app.config.settings import get_settings
    from app.executors.process import local_command
    from app.runtime.opencode_server import OpenCodeServerManager

    return get_settings(), OpenCodeServerManager, local_command


def state_path(settings: Any) -> Path:
    return settings.resolved_runtime_dir / "astra-service.json"


def process_exists(pid: int) -> bool:
    return procman.process_exists(pid)


def load_state(settings: Any) -> dict[str, Any]:
    path = state_path(settings)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(settings: Any, mode: str, processes: dict[str, dict[str, Any]]) -> None:
    path = state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mode": mode,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "processes": processes,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def clear_state(settings: Any) -> None:
    path = state_path(settings)
    if path.exists():
        path.unlink()


def managed_processes(settings: Any) -> dict[str, dict[str, Any]]:
    processes = load_state(settings).get("processes", {})
    return processes if isinstance(processes, dict) else {}


def spawn(
    settings: Any,
    name: str,
    command: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings.resolved_log_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.resolved_log_dir / f"{name}.log"
    return procman.spawn_process(command, cwd, log_path, env=env)


def rollback(processes: dict[str, dict[str, Any]]) -> None:
    for info in reversed(list(processes.values())):
        try:
            procman.stop_managed(info)
        except Exception:
            pass


def run_checked(command: list[str], cwd: Path, title: str) -> None:
    print(f"[PREP] {title}")
    result = subprocess.run(command, cwd=cwd, env=os.environ.copy(), check=False)
    if result.returncode:
        raise RuntimeError(f"{title} failed (exit={result.returncode})")


def find_npm() -> str:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm") or shutil.which("npm")
    if not npm:
        raise RuntimeError("npm not found; install Node.js 20+")
    return npm


def upstream_workspace_ui_version() -> str:
    """返回 workspace-ui 固定的上游 OpenCode 版本（UPSTREAM.json tag）。

    UPSTREAM.json 是固定上游 UI 产物的唯一来源；缺失或损坏时必须显式失败，
    不能静默跳过启动时的固定上游版本校验。
    """

    path = ROOT / "web" / "workspace-ui" / "UPSTREAM.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"workspace-ui upstream metadata unreadable: {path} ({exc})"
        ) from exc
    tag = data.get("tag") if isinstance(data, dict) else None
    version = str(tag).strip().removeprefix("v") if isinstance(tag, str) else ""
    if not version:
        raise RuntimeError(f"workspace-ui upstream metadata missing valid 'tag': {path}")
    return version


def ensure_workspace_ui_bundle(version: str | None) -> None:
    """运行中的 OpenCode 版本必须有对应的 Workspace UI，不能回落到 Nexus 页面。"""

    cleaned = str(version or "").strip().removeprefix("v")
    if not cleaned or cleaned == "unknown":
        raise RuntimeError("OpenCode did not report a version; cannot select a Workspace UI bundle")
    path = FRONTEND / "public" / "opencode-workspace" / cleaned / "index.html"
    if not path.is_file():
        raise RuntimeError(
            f"workspace-ui bundle missing for running OpenCode {cleaned}: {path}; "
            "restore the committed bundle from the repository, or rebuild that version "
            "with: node tools/e2e/workspace_ui.mjs build"
        )


def missing_workspace_ui_bundles(settings: Any) -> list[Path]:
    """返回启动所需但缺失的 Workspace UI。只要求 UPSTREAM.json 固定的那一个版本。"""

    del settings
    version = upstream_workspace_ui_version()
    bundle = FRONTEND / "public" / "opencode-workspace" / version / "index.html"
    return [] if bundle.is_file() else [bundle]


def validate_start(mode: str, settings: Any) -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("Astra requires Python 3.11+")
    if not (ROOT / "ops" / "env" / ".env").is_file():
        raise RuntimeError("env file not found; expected ops/env/.env")
    if not (FRONTEND / "node_modules").is_dir():
        raise RuntimeError("web dependencies missing; run: cd services/nexus-api/web && npm ci")
    missing = missing_workspace_ui_bundles(settings)
    if missing:
        detail = "; ".join(str(path) for path in missing)
        raise RuntimeError(
            f"workspace-ui bundle missing: {detail}; "
            "restore the committed bundle from the repository, or rebuild the "
            "configured upstream version with: node tools/e2e/workspace_ui.mjs build"
        )

    if mode == "pro":
        database_url = (settings.database_url or "").lower()
        sqlite = database_url.startswith("sqlite") if database_url else settings.database_backend == "sqlite"
        if sqlite:
            raise RuntimeError("production mode requires MySQL")
        if settings.executor_type != "opencode_server":
            raise RuntimeError("production mode requires ASTRA_EXECUTOR_TYPE=opencode_server")
        validate_gateway_production(settings)


def validate_gateway_production(settings: Any) -> None:
    """正式模式启用 API 中转时，强制校验数据面依赖，不靠回退掩盖缺失配置。"""

    if not settings.gateway_enabled:
        return
    if not settings.gateway_master_key:
        raise RuntimeError(
            "production mode requires ASTRA_GATEWAY_MASTER_KEY when API gateway is enabled"
        )
    if not (settings.redis_url or "").strip():
        raise RuntimeError(
            "API gateway requires a configured Redis (ASTRA_REDIS_URL); "
            "in-process counters are not a fallback"
        )


def prepare(mode: str, settings: Any, npm: str, local_command: Any) -> None:
    validate_start(mode, settings)
    run_checked([sys.executable, "-m", "alembic", "upgrade", "head"], BACKEND, "database migration")
    run_checked([sys.executable, str(ROOT / "ops" / "dev" / "preflight.py")], ROOT, "preflight")
    if mode == "pro":
        run_checked(local_command(npm, ["run", "build"]), FRONTEND, "frontend production build")
        if not (FRONTEND / "dist" / "index.html").is_file():
            raise RuntimeError("services/nexus-api/web/dist/index.html not found after build")


def http_ready(url: str, *, headers: dict[str, str] | None = None) -> bool:
    return procman.http_ready(url, headers=headers)


def wait_http(
    name: str,
    url: str,
    timeout: int = 45,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    procman.wait_http(name, url, timeout=timeout, headers=headers)


def wait_process(name: str, info: dict[str, Any]) -> None:
    time.sleep(0.5)
    if not process_exists(int(info["pid"])):
        raise RuntimeError(f"{name} exited; log={info['log']}")


def wait_worker(name: str, info: dict[str, Any], timeout: int = 60) -> None:
    deadline = time.monotonic() + timeout
    log_path = Path(info["log"])
    while time.monotonic() < deadline:
        if not process_exists(int(info["pid"])):
            raise RuntimeError(f"{name} exited; log={log_path}")
        try:
            log = log_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            log = ""
        if "ready." in log:
            print(f"[READY] {name}")
            return
        time.sleep(0.5)
    raise RuntimeError(f"{name} not ready; log={log_path}")


def start_opencode(
    settings: Any,
    OpenCodeServerManager: Any,
    processes: dict[str, dict[str, Any]],
) -> None:
    if settings.executor_type == "opencode_server":
        manager = OpenCodeServerManager(settings)
        health = asyncio.run(manager.health())
        if not health.healthy and not settings.opencode_server_url:
            print("[START] opencode-server")
            processes["opencode-server"] = spawn(
                settings,
                "opencode-server",
                manager.command(),
                ROOT,
                env=manager.process_environment(),
            )
        health = asyncio.run(manager.wait_until_ready())
        ensure_workspace_ui_bundle(health.version)
        print(f"[READY] employee OpenCode Server ({health.version or 'unknown'})")



def gateway_command(mode: str) -> list[str]:
    """独立 Gateway 数据面进程：只承载 /v1 转发，与任务 Runtime 隔离。"""

    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.gateway.server:app",
        "--host",
        GATEWAY_HOST,
        "--port",
        str(GATEWAY_PORT),
    ]
    if mode == "dev" and os.name != "nt":
        command.append("--reload")
    return command


def frontend_command(mode: str, npm: str, local_command: Any) -> list[str]:
    script = "dev" if mode == "dev" else "preview"
    return local_command(
        npm,
        ["run", script, "--", "--host", FRONTEND_HOST, "--port", str(FRONTEND_PORT)],
    )


def task_mcp_command() -> list[str]:
    """Task MCP 入口。优先用服务虚拟环境里的 astra-task-mcp。"""

    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "astra-task-mcp.exe" if os.name == "nt" else "astra-task-mcp"
    binary = BACKEND / ".venv" / scripts / name
    if binary.is_file():
        return [str(binary)]
    return [sys.executable, "-c", "from app.task_mcp import main; main()"]


def core_services(
    mode: str,
    settings: Any,
    npm: str,
    local_command: Any,
) -> list[tuple[str, list[str], Path, dict[str, str] | None]]:
    """核心受管服务清单：Celery、Task MCP、可选 Library MCP/Gateway，开发模式再加 Vite。

    只组装命令，不做启动与就绪等待，便于脚本级校验。Gateway 仅在
    ``ASTRA_GATEWAY_ENABLED=true`` 时作为独立进程加入；长连接不进入 Backend。
    """

    schedule = settings.resolved_runtime_dir / "celerybeat-schedule"
    services: list[tuple[str, list[str], Path, dict[str, str] | None]] = [
        (
            "runtime-beat",
            [
                sys.executable, "-m", "celery", "-A", "app.runtime.celery_app:celery",
                "beat", "--loglevel=INFO", f"--schedule={schedule}",
            ],
            BACKEND,
            None,
        ),
        (
            "runtime-control",
            [
                sys.executable, "-m", "celery", "-A", "app.runtime.celery_app:celery",
                "worker", "--loglevel=INFO", "--pool=solo", "--queues=nexus-control",
                "--hostname=astra-control@%h",
            ],
            BACKEND,
            None,
        ),
        (
            "runtime-agent",
            [
                sys.executable, "-m", "celery", "-A", "app.runtime.celery_app:celery",
                "worker", "--loglevel=INFO", "--pool=solo", "--queues=agent",
                "--hostname=astra-agent@%h",
            ],
            BACKEND,
            None,
        ),
        (
            "runtime-review",
            [
                sys.executable, "-m", "celery", "-A", "app.runtime.celery_app:celery",
                "worker", "--loglevel=INFO", "--concurrency=2", "--queues=nexus-review",
                "--hostname=nexus-review@%h",
            ],
            BACKEND,
            None,
        ),
    ]

    services.append(("task-mcp", task_mcp_command(), BACKEND, None))

    if settings.mcp_transport == "streamable-http":
        services.append(("library-mcp", [sys.executable, "-m", "app.mcp_server"], BACKEND, None))

    # 只加载最近已发布配置，启动时不发布草稿。
    if settings.gateway_enabled:
        services.append(("gateway", gateway_command(mode), BACKEND, None))

    frontend_env = os.environ.copy()
    frontend_env["VITE_PROXY_TARGET"] = f"http://127.0.0.1:{BACKEND_PORT}"
    frontend_env["VITE_GATEWAY_PROXY_TARGET"] = f"http://127.0.0.1:{GATEWAY_PORT}"
    frontend_env["VITE_AGENTSCOPE_PROXY_TARGET"] = os.environ.get(
        "NEXUS_AGENTSCOPE_BASE_URL", "http://127.0.0.1:18000"
    )
    if mode == "dev":
        services.append(
            ("frontend", frontend_command(mode, npm, local_command), FRONTEND, frontend_env)
        )
    return services


def start_core(
    mode: str,
    settings: Any,
    npm: str,
    local_command: Any,
    processes: dict[str, dict[str, Any]],
) -> None:
    services = core_services(mode, settings, npm, local_command)

    for name, command, cwd, env in services:
        print(f"[START] {name}")
        processes[name] = spawn(settings, name, command, cwd, env=env)
        wait_process(name, processes[name])

    wait_worker("runtime-control", processes["runtime-control"])
    wait_worker("runtime-agent", processes["runtime-agent"])
    wait_worker("runtime-review", processes["runtime-review"])
    if "gateway" in processes:
        wait_http("gateway", f"http://127.0.0.1:{GATEWAY_PORT}/healthz")
    if "task-mcp" in processes:
        wait_http("task-mcp", f"http://127.0.0.1:{TASK_MCP_PORT}/health")
    if "frontend" in processes:
        wait_http("frontend", f"http://127.0.0.1:{FRONTEND_PORT}")


def status(settings: Any) -> int:
    state = load_state(settings)
    processes = state.get("processes", {})
    if not processes:
        print("Astra is not running.")
        return 1

    print(f"Astra mode: {state.get('mode', 'unknown')}")
    healthy = True
    for name, info in processes.items():
        running = process_exists(int(info["pid"]))
        healthy = healthy and running
        print(
            f"[{'RUNNING' if running else 'STOPPED':7}] "
            f"{name:<20} PID={info['pid']} LOG={info['log']}"
        )
    return 0 if healthy else 1


def stop(settings: Any) -> int:
    processes = managed_processes(settings)
    if not processes:
        clear_state(settings)
        print("Astra is not running.")
        return 0

    remaining: dict[str, dict[str, Any]] = {}
    for name, info in reversed(list(processes.items())):
        if procman.release_record(info, label=name) == "keep":
            remaining[name] = info
    if remaining:
        mode = str(load_state(settings).get("mode") or "dev")
        save_state(settings, mode, remaining)
        print("Astra did not stop completely.")
        return 1
    clear_state(settings)
    print("Astra stopped.")
    return 0


def _blocks_start(info: dict[str, Any]) -> bool:
    pid = int(info["pid"])
    raw_pgid = info.get("pgid")
    pgid = int(raw_pgid) if raw_pgid is not None else None
    procman.reap(pid, pgid)
    if procman.leader_live(pid):
        return procman.identify_process(info, pid) in {"ours", "unknown"}
    if pgid == pid and procman.group_alive(pid):
        return True
    port = procman.listening_port(info)
    return port is not None and procman.port_in_use(port)


def _provision_gateway_admin_key() -> None:
    """在拉起 API 之前生成一次管理密钥。失败只告警，不阻止其余服务启动。"""

    try:
        from app.worker_gateway.admin_key import provision_worker_gateway_admin_key

        provision_worker_gateway_admin_key(ROOT / "ops" / "env" / ".env")
    except Exception as exc:
        print(f"[WARN] worker gateway admin key was not provisioned: {exc}")


def start(
    mode: str,
    settings: Any,
    OpenCodeServerManager: Any,
    local_command: Any,
) -> int:
    existing = managed_processes(settings)
    if any(_blocks_start(info) for info in existing.values()):
        print("Astra already has managed services running; use restart.")
        return status(settings)

    clear_state(settings)
    processes: dict[str, dict[str, Any]] = {}
    try:
        npm = find_npm()
        prepare(mode, settings, npm, local_command)
        start_opencode(settings, OpenCodeServerManager, processes)
        _provision_gateway_admin_key()
        start_core(mode, settings, npm, local_command, processes)
        save_state(settings, mode, processes)
    except KeyboardInterrupt:
        print("\n[ERROR] startup interrupted; rolling back...")
        rollback(processes)
        clear_state(settings)
        return 130
    except Exception as exc:
        print(f"[ERROR] Astra startup failed: {exc}")
        rollback(processes)
        clear_state(settings)
        return 1

    print()
    print("=" * 64)
    print(f"Astra {mode.upper()} started")
    if mode == "dev":
        print(f"Console : http://localhost:{FRONTEND_PORT}")
    print(f"API     : http://localhost:{BACKEND_PORT}/api/health")
    print(f"Docs    : http://localhost:{BACKEND_PORT}/docs")
    if settings.gateway_enabled:
        print(f"Gateway : http://localhost:{GATEWAY_PORT}/v1/models")
    print(f"Task MCP: http://127.0.0.1:{TASK_MCP_PORT}/mcp")
    print(f"Logs    : {settings.resolved_log_dir}")
    print("=" * 64)
    return status(settings)


def main() -> int:
    args = parse_args()
    try:
        settings, OpenCodeServerManager, local_command = load_astra(args.mode)
    except Exception as exc:
        print(f"[ERROR] failed to load Astra environment: {exc}")
        return 1

    if args.action == "status":
        return status(settings)
    if args.action == "stop":
        return stop(settings)
    if args.action == "restart":
        stopped = stop(settings)
        if stopped != 0:
            return stopped
    return start(
        args.mode,
        settings,
        OpenCodeServerManager,
        local_command,
    )


if __name__ == "__main__":
    raise SystemExit(main())
