#!/usr/bin/env python3
import argparse
import asyncio
import importlib
import json
import platform
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = PROJECT_ROOT / "services" / "task-platform"
sys.path.insert(0, str(BACKEND_ROOT))


@dataclass
class CheckResult:
    item: str
    passed: bool
    current: str
    reason: str
    suggestion: str
    blocking: bool = True


def result(
    item: str,
    passed: bool,
    current: object,
    reason: str,
    suggestion: str,
    *,
    blocking: bool = True,
) -> CheckResult:
    return CheckResult(item, passed, str(current), reason, suggestion, blocking)


def check_python() -> CheckResult:
    version = platform.python_version()
    passed = sys.version_info >= (3, 11)
    return result(
        "Python 版本",
        passed,
        f"{sys.executable} ({version})",
        "版本满足项目要求" if passed else "Astra 当前代码基线需要 Python 3.11+",
        "无需处理" if passed else "使用 Python 3.11 或更高版本重建 .venv",
    )


def check_operating_system() -> CheckResult:
    current = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return result("当前操作系统", True, current, "已识别运行平台", "无需处理")


def check_dependencies() -> CheckResult:
    modules = {
        "alembic": "alembic",
        "asyncmy": "asyncmy",
        "celery": "celery",
        "fastapi": "fastapi",
        "httpx": "httpx",
        "paramiko": "paramiko",
        "redis": "redis",
        "sqlalchemy": "sqlalchemy",
        "uvicorn": "uvicorn",
    }
    missing = []
    for label, module in modules.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(label)
    return result(
        "Python 必要依赖",
        not missing,
        "已安装" if not missing else f"缺少: {', '.join(missing)}",
        "依赖完整" if not missing else "当前解释器未安装全部后端依赖",
        "无需处理" if not missing else "执行 python -m pip install -e services/task-platform",
    )


def check_writable_directory(item: str, path: Path) -> CheckResult:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".astra-check-", dir=path, delete=False) as probe:
            probe_path = Path(probe.name)
            probe.write(b"ok")
        probe_path.unlink()
        return result(item, True, path, "目录存在且可写", "无需处理")
    except OSError as exc:
        return result(
            item,
            False,
            path,
            str(exc),
            "创建该目录，并给 Astra 运行账号读写权限",
        )


def astra_table_names() -> set[str]:
    # Mirror alembic/env.py: import every model module so all Astra tables are
    # registered on the shared metadata before the isolation check runs.
    import app.assistant.models  # noqa: F401 - register assistant tables on shared metadata
    import app.gateway.models  # noqa: F401 - register gateway tables on shared metadata
    from app.models import Base

    return set(Base.metadata.tables)


def foreign_table_names(tables: set[str]) -> list[str]:
    return sorted(tables - astra_table_names() - {"alembic_version"})


async def check_database(settings) -> list[CheckResult]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text
    from sqlalchemy.ext.asyncio import create_async_engine

    checks = []
    engine_options = {"pool_pre_ping": True}
    if settings.sqlalchemy_database_url.startswith("mysql"):
        engine_options["pool_recycle"] = settings.database_pool_recycle
    engine = None
    try:
        engine = create_async_engine(settings.sqlalchemy_database_url, **engine_options)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            checks.append(
                result(
                    "数据库连接",
                    True,
                    settings.database_description,
                    "连接成功",
                    "无需处理",
                )
            )

            if settings.sqlalchemy_database_url.startswith("mysql"):
                row = (
                    await connection.execute(
                        text(
                            "SELECT @@version, @@character_set_database, "
                            "@@character_set_connection, @@collation_database"
                        )
                    )
                ).one()
                charset_ok = all(str(value).lower() == "utf8mb4" for value in row[1:3])
                checks.append(
                    result(
                        "MySQL 字符集",
                        charset_ok,
                        f"MySQL {row[0]} / database={row[1]} / connection={row[2]} / {row[3]}",
                        "数据库与连接字符集均为 utf8mb4" if charset_ok else "数据库或连接字符集不是 utf8mb4",
                        "无需处理" if charset_ok else "将 Astra 数据库和连接字符集调整为 utf8mb4",
                    )
                )

            def schema_state(sync_connection):
                inspector = inspect(sync_connection)
                tables = set(inspector.get_table_names())
                version = None
                if "alembic_version" in tables:
                    version = sync_connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalar_one_or_none()
                return tables, version

            tables, current_revision = await connection.run_sync(schema_state)
            config = Config(str(BACKEND_ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
            head_revision = ScriptDirectory.from_config(config).get_current_head()
            required_tables = {
                "agent_workers",
                "events",
                "opencode_execution_environments",
                "runtime_sessions",
                "task_categories",
                "task_summaries",
                "tasks",
            }
            missing_tables = sorted(required_tables - tables)
            migration_ok = not missing_tables and current_revision == head_revision
            foreign_tables = foreign_table_names(tables)
            checks.append(
                result(
                    "数据库隔离",
                    not foreign_tables,
                    f"非 Astra 表={len(foreign_tables)}",
                    "当前数据库仅包含 Astra 表"
                    if not foreign_tables
                    else f"检测到共享数据库表，例如：{', '.join(foreign_tables[:5])}",
                    "无需处理"
                    if not foreign_tables
                    else "建议为 Astra 使用独立 Database；共享库外部长事务可能阻塞 Astra 写操作",
                    blocking=False,
                )
            )

            checks.append(
                result(
                    "数据库表与迁移",
                    migration_ok,
                    f"revision={current_revision or '未初始化'}, tables={len(tables)}",
                    "表结构与迁移已到最新"
                    if migration_ok
                    else f"缺少表: {missing_tables or '无'}; head={head_revision}",
                    "无需处理" if migration_ok else "在 backend 目录执行 python -m alembic upgrade head",
                )
            )
    except Exception as exc:  # noqa: BLE001 - convert driver errors into actionable checks
        detail = str(exc).replace(settings.sqlalchemy_database_url, settings.database_description)
        checks.append(
            result(
                "数据库连接",
                False,
                settings.database_description,
                detail,
                "检查 MySQL 服务、Host、Port、Database、Username、Password 和网络权限",
            )
        )
        checks.append(
            result(
                "数据库表与迁移",
                False,
                "未检查",
                "数据库连接失败",
                "数据库连接恢复后执行 python -m alembic upgrade head",
            )
        )
    finally:
        if engine is not None:
            await engine.dispose()
    return checks


async def check_redis(settings) -> CheckResult:
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, socket_connect_timeout=3)
        try:
            await client.ping()
        finally:
            await client.aclose()
        return result("Redis 连接", True, settings.redis_description, "连接成功", "无需处理")
    except Exception as exc:  # noqa: BLE001 - Redis clients expose multiple error types
        return result(
            "Redis 连接",
            False,
            settings.redis_description,
            str(exc).replace(settings.redis_url, settings.redis_description),
            "检查 ASTRA_REDIS_URL 及 Redis 服务；Windows 可连接公司统一 Redis",
        )


async def check_agent(settings) -> CheckResult:
    from app.executors.process import (
        local_command,
        process_group_options,
        resolve_executable,
        terminate_process,
    )

    resolved = resolve_executable(settings.opencode_binary)
    if not resolved:
        return result(
            "默认 Agent 可执行文件",
            False,
            settings.opencode_binary,
            "PATH 和配置路径中都未找到可执行文件",
            "设置 ASTRA_OPENCODE_BINARY 为 opencode.exe 绝对路径，或将 OpenCode 加入 PATH",
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *local_command(resolved, ["--version"]),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_group_options(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError:
            await terminate_process(process)
            return result(
                "默认 Agent 可执行文件",
                False,
                resolved,
                "执行 --version 超过 15 秒",
                "确认 Agent 能在当前账号下非交互启动，并检查模型配置或网络",
            )
        detail = output.decode("utf-8", errors="replace").strip()[:500]
        passed = process.returncode == 0
        return result(
            "默认 Agent 可执行文件",
            passed,
            resolved,
            detail or f"退出码 {process.returncode}",
            "无需处理" if passed else "确认该文件可执行且 --version 能正常返回",
        )
    except Exception as exc:  # noqa: BLE001 - process launch errors vary by operating system
        return result(
            "默认 Agent 可执行文件",
            False,
            resolved,
            str(exc),
            "确认路径无误，并允许 Astra 运行账号启动该程序",
        )


async def check_opencode_runtime(settings) -> CheckResult:
    if settings.executor_type == "opencode_cli_fallback":
        return result(
            "OpenCode Runtime",
            True,
            "opencode_cli_fallback",
            "已显式选择隔离的 CLI fallback",
            "生产环境建议使用 opencode_server",
        )

    from app.runtime.opencode_server import OpenCodeServerManager

    manager = OpenCodeServerManager(settings)
    health = await manager.health()
    if health.healthy:
        return result(
            "OpenCode Runtime",
            True,
            manager.base_url,
            f"OpenCode Server 已就绪，版本 {health.version or '未知'}",
            "无需处理",
        )
    if not settings.opencode_server_url:
        return result(
            "OpenCode Runtime",
            True,
            manager.base_url,
            "本机受管 Server 将由 Astra 启动器启动并执行健康检查",
            "无需处理",
        )
    return result(
        "OpenCode Runtime",
        False,
        manager.base_url,
        f"外部 OpenCode Server 不可用：{health.detail}",
        "启动 ASTRA_OPENCODE_SERVER_URL 指定的 Server 并检查认证",
    )


async def check_assistant_runtime(settings) -> CheckResult:
    if not settings.assistant_enabled:
        return result(
            "平台助手 Runtime",
            True,
            "disabled",
            "已显式关闭平台助手",
            "无需处理",
        )

    from app.assistant.server import AssistantOpenCodeServerManager

    manager = AssistantOpenCodeServerManager(settings)
    project_root = manager.workspace
    agent_path = project_root / ".opencode" / "agents" / "platform-assistant.md"
    mcp_configured = False
    config_path = project_root / "opencode.json"
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            mcp_configured = "nexus-assistant" in (config.get("mcp") or {})
        except (OSError, ValueError):
            mcp_configured = False
    if not agent_path.is_file() or not mcp_configured:
        return result(
            "平台助手 Runtime",
            False,
            project_root,
            "项目级助手 Agent 或 nexus-assistant MCP 未配置",
            "确认 .opencode/agents/platform-assistant.md 存在，且 opencode.json 配置了 nexus-assistant MCP",
        )
    entrypoint = manager.mcp_entrypoint
    if not entrypoint.is_file():
        return result(
            "平台助手 Runtime",
            False,
            entrypoint,
            "当前 Python 环境中缺少助手 MCP 入口",
            "使用当前 Python 执行 python -m pip install -e services/task-platform",
        )

    health = await manager.health()
    if health.healthy:
        return result(
            "平台助手 Runtime",
            True,
            manager.base_url,
            f"助手 OpenCode Server 已就绪，版本 {health.version or '未知'}",
            "无需处理",
        )
    if not settings.assistant_opencode_url:
        return result(
            "平台助手 Runtime",
            True,
            manager.base_url,
            "本机受管助手 Server 将由 Astra 启动器启动并执行健康检查",
            "无需处理",
        )
    return result(
        "平台助手 Runtime",
        False,
        manager.base_url,
        f"外部助手 OpenCode Server 不可用：{health.detail}",
        "启动 ASTRA_ASSISTANT_OPENCODE_URL 指定的 Server 并检查认证",
    )


def print_results(checks: list[CheckResult]) -> None:
    for check in checks:
        marker = "PASS" if check.passed else "FAIL" if check.blocking else "WARN"
        print(f"[{marker}] {check.item}")
        print(f"  当前值: {check.current}")
        print(f"  原因: {check.reason}")
        print(f"  建议: {check.suggestion}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Astra 跨平台环境自检")
    parser.parse_args()

    checks = [check_operating_system(), check_python(), check_dependencies()]
    if not checks[-1].passed:
        print_results(checks)
        return 1

    from app.config.settings import PROJECT_ROOT, get_settings

    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - present invalid settings without traceback
        if hasattr(exc, "errors"):
            details = "; ".join(
                f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
                for error in exc.errors(include_input=False)
            )
        else:
            details = exc.__class__.__name__
        checks.append(
            result(
                "Astra 配置",
                False,
                PROJECT_ROOT / "ops" / "env" / ".env",
                details,
                "检查 ops/env/.env 的变量名称、格式和值；可重新复制 ops/env/.env.example",
            )
        )
        print_results(checks)
        return 1
    checks.extend(
        [
            check_writable_directory("Astra Workspace 目录", settings.resolved_workspace_root),
            check_writable_directory("日志目录", settings.resolved_log_dir),
            check_writable_directory("运行状态目录", settings.resolved_runtime_dir),
            await check_agent(settings),
            await check_opencode_runtime(settings),
            await check_assistant_runtime(settings),
            await check_redis(settings),
        ]
    )
    checks.extend(await check_database(settings))
    print_results(checks)
    failures = sum(not check.passed and check.blocking for check in checks)
    warnings = sum(not check.passed and not check.blocking for check in checks)
    passed = len(checks) - failures - warnings
    print(f"\n自检完成：{passed} 项通过，{warnings} 项警告，{failures} 项失败。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
