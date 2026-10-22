#!/usr/bin/env python3
"""Check this checkout's defaults against the names v0.9 binds on one host.

The script does not start v0.9. It ignores ASTRA_* and ops/env/.env so a
developer override cannot hide a default collision. Exit 1 when any name overlaps.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
TASK_PLATFORM = ROOT / "services" / "nexus-api"

# Values v0.9 binds, taken from the defaults this branch moved away from.
V09_DATABASE_NAME = "astra"
V09_DATABASE_USER = "astra"
V09_SQLITE_FILE = "astra.db"
V09_REDIS_DB = "0"
V09_REDIS_PREFIXES = ("astra:worker-gateway", "astra:gateway:")
V09_CELERY_APP = "astra"
V09_CELERY_QUEUE = "control"
V09_CELERY_TASK_PREFIX = "astra."
V09_UNITS = ("astra-opencode-", "astra-worker-")
V09_OPT = "/opt/astra"
V09_ETC = "/etc/astra"
V09_LOCK_DIR = "astra-employee-serve"
V09_PORTS = {
    "agent": 8000,
    "task_api": 8100,
    "gateway": 8110,
    "nexus_api": 8120,
    "library_mcp": 8201,
    "task_mcp": 8202,
    "web": 5174,
    "opencode": 4096,
}


def _clear_astra_env() -> None:
    for key in list(os.environ):
        if key.startswith("ASTRA_"):
            del os.environ[key]


def _load_astra_service():
    path = ROOT / "ops" / "dev" / "astra_service.py"
    spec = importlib.util.spec_from_file_location("astra_service_defaults", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _port_from_url(value: str) -> int:
    return int(urlparse(value).port or 0)


def collect() -> list[tuple[bool, str, str]]:
    _clear_astra_env()
    sys.path.insert(0, str(TASK_PLATFORM))

    from app.config.settings import Settings
    from app.executors.opencode_local_server import SERVE_LOCK_DIR
    from app.gateway.runtime import LEASE_KEY, REVOKED_KEYS_KEY
    from app.runtime.celery_app import celery
    from app.runtime.remote_provisioning import WORKER_SECRET_DIR, agent_venv, worker_install_dir
    from app.worker_gateway.bus import RedisRouteBus

    settings = Settings(_env_file=None)
    service = _load_astra_service()
    install = (ROOT / "ops" / "deploy" / "install_nexus.sh").read_text(encoding="utf-8")
    provisioning = (
        TASK_PLATFORM / "app" / "runtime" / "remote_provisioning.py"
    ).read_text(encoding="utf-8")
    install_script = (TASK_PLATFORM / "app" / "runtime" / "remote_install.sh").read_text(encoding="utf-8")
    channel_switch = (
        TASK_PLATFORM / "app" / "worker_agent" / "channel_switch.py"
    ).read_text(encoding="utf-8")
    rows: list[tuple[bool, str, str]] = []

    def add(ok: bool, label: str, detail: str) -> None:
        rows.append((ok, label, detail))

    add(
        settings.database_name == "nexus" and settings.database_name != V09_DATABASE_NAME,
        "database name",
        settings.database_name,
    )
    add(
        settings.database_username == "nexus" and settings.database_username != V09_DATABASE_USER,
        "database user",
        settings.database_username,
    )
    sqlite_name = settings.sqlalchemy_database_url.rsplit("/", 1)[-1]
    add(sqlite_name == "nexus.db" and sqlite_name != V09_SQLITE_FILE, "sqlite file", sqlite_name)
    add(
        str(settings.database_description).endswith("nexus.db"),
        "database description",
        Path(str(settings.database_description)).name,
    )

    redis_db = urlparse(settings.redis_url).path.strip("/")
    prefix = inspect.signature(RedisRouteBus.__init__).parameters["prefix"].default
    add(redis_db == "1" and redis_db != V09_REDIS_DB, "redis db", redis_db)
    add(
        prefix == "nexus:worker-gateway" and prefix not in V09_REDIS_PREFIXES,
        "redis prefix",
        prefix,
    )
    add(
        LEASE_KEY.startswith("nexus:gateway:") and REVOKED_KEYS_KEY.startswith("nexus:gateway:"),
        "redis gateway prefix",
        LEASE_KEY.split("{", 1)[0],
    )

    task_names = [name for name in celery.tasks if name.startswith(V09_CELERY_TASK_PREFIX)]
    queues = {route["queue"] for route in celery.conf.task_routes.values()}
    add(celery.main == "nexus" and celery.main != V09_CELERY_APP, "celery app", celery.main)
    add(
        celery.conf.task_default_queue == "nexus-control"
        and celery.conf.task_default_queue != V09_CELERY_QUEUE
        and queues <= {"nexus-control", "nexus-review"}
        and "nexus-control" in queues
        and not task_names,
        "celery queue",
        celery.conf.task_default_queue,
    )

    # The wheel name astra-worker-agent is the package name, not a systemd unit.
    unit_text = provisioning.replace("astra-worker-agent", "")
    unit_hit = [name for name in V09_UNITS if name in unit_text]
    add(
        "nexus-opencode-" in provisioning and "nexus-worker-" in provisioning and not unit_hit,
        "systemd units",
        "nexus-opencode-{id}.service, nexus-worker-{id}.service",
    )

    path_hit = V09_OPT in provisioning or V09_ETC in provisioning
    add(
        agent_venv(1).startswith("/opt/nexus/")
        and worker_install_dir(1).startswith("/opt/nexus/")
        and WORKER_SECRET_DIR == "/etc/nexus"
        and "NEXUS_ROOT:=/opt/nexus" in install
        and not path_hit,
        "opt and etc paths",
        f"{agent_venv(1)}, {WORKER_SECRET_DIR}",
    )

    nexus_ports = {
        "agent": _port_from_url(settings.agentscope_base_url),
        "task_api": service.BACKEND_PORT,
        "gateway": service.GATEWAY_PORT,
        "nexus_api": _port_from_url(settings.nexus_api_base),
        "library_mcp": settings.mcp_port,
        "task_mcp": settings.task_mcp_port,
        "schedule_mcp": 18203,
        "web": service.FRONTEND_PORT,
        "opencode": settings.opencode_server_port,
    }
    occupied = set(V09_PORTS.values())
    shared = sorted(port for port in nexus_ports.values() if port in occupied)
    # Extract literal defaults; configured live ports are already checked above.
    listed = [int(item) for item in re.findall(
        r"\d+", re.search(r"^PORTS=\(([^)]*)\)", install, re.M).group(1)
    )]
    install_shared = sorted(port for port in listed if port != 6379 and port in occupied)
    add(
        not shared and not install_shared and nexus_ports["opencode"] != V09_PORTS["opencode"],
        "ports",
        " ".join(f"{name}={port}" for name, port in nexus_ports.items()),
    )

    add(
        SERVE_LOCK_DIR == "nexus-employee-serve" and SERVE_LOCK_DIR != V09_LOCK_DIR,
        "lock directory",
        SERVE_LOCK_DIR,
    )
    add(
        ".cache/nexus-provision" in provisioning
        and ".cache/nexus-provision" in install_script
        and ".cache/astra-provision" not in provisioning
        and ".cache/astra-provision" not in install_script,
        "provision cache",
        "nexus-provision",
    )
    add(
        "/etc/nexus/opencode/" in provisioning
        and "Environment=OPENCODE_CONFIG=" in provisioning
        and "OPENCODE_CONFIG" in channel_switch
        and "${XDG_CONFIG_HOME:-$HOME/.config}" not in provisioning
        and "${XDG_CONFIG_HOME:-$HOME/.config}" not in install_script,
        "opencode config",
        "/etc/nexus/opencode/{id}/opencode.jsonc",
    )
    return rows


def main() -> int:
    rows = collect()
    failed = False
    for ok, label, detail in rows:
        print(f"{'ok' if ok else 'collide'}  {label}: {detail}")
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
