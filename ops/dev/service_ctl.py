#!/usr/bin/env python3
"""Start and stop the single-process Nexus services.

The dev shell scripts are wrappers around this program. It records pid, pgid,
psutil create_time, and cwd, and it stops a process only when pid and
create_time still match.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _load_procman():
    path = Path(__file__).resolve().parent / "procman.py"
    spec = importlib.util.spec_from_file_location("service_ctl_procman", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


procman = _load_procman()

ROOT = Path(__file__).resolve().parents[2]


def state_path() -> Path:
    override = os.environ.get("SERVICE_CTL_STATE_DIR")
    directory = Path(override) if override else ROOT / ".astra" / "run"
    return directory / "service-ctl.json"


def load_processes() -> dict[str, dict[str, Any]]:
    path = state_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    processes = value.get("processes", {}) if isinstance(value, dict) else {}
    return processes if isinstance(processes, dict) else {}


def save_processes(processes: dict[str, dict[str, Any]]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"processes": processes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _python(path: Path) -> str:
    if path.is_file():
        return str(path)
    raise RuntimeError(f"missing interpreter: {path}")


def agent_command() -> list[str]:
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "python.exe" if os.name == "nt" else "python"
    py = ROOT / "services" / "agent-service" / ".venv-agentscope" / scripts / name
    return [_python(py), str(ROOT / "services" / "agent-service" / "agent_service.py")]


def task_mcp_command() -> list[str]:
    scripts = "Scripts" if os.name == "nt" else "bin"
    binary_name = "astra-task-mcp.exe" if os.name == "nt" else "astra-task-mcp"
    binary = ROOT / "services" / "nexus-api" / ".venv" / scripts / binary_name
    if binary.is_file():
        return [str(binary)]
    py_name = "python.exe" if os.name == "nt" else "python"
    py = ROOT / "services" / "nexus-api" / ".venv" / scripts / py_name
    return [_python(py), "-c", "from app.task_mcp import main; main()"]


def nexus_api_command() -> list[str]:
    scripts = "Scripts" if os.name == "nt" else "bin"
    binary_name = "uvicorn.exe" if os.name == "nt" else "uvicorn"
    uvicorn = ROOT / "services" / "nexus-api" / ".venv" / scripts / binary_name
    return [
        _python(uvicorn),
        "nexus_api.service:app",
        "--app-dir",
        str(ROOT / "services" / "nexus-api"),
        "--host",
        "127.0.0.1",
        "--port",
        os.environ.get("NEXUS_API_PORT", "18120"),
    ]


def _env_port(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


SERVICES: dict[str, dict[str, Any]] = {
    "agent": {
        "cwd": ROOT / "services" / "agent-service",
        "log_env": "AGENT_SERVICE_LOG",
        "default_log": "/tmp/nexus-agent-service.log",
        "command": agent_command,
        "port": lambda: _env_port("AGENTSCOPE_PORT", 18000),
        "health": lambda port: f"http://127.0.0.1:{port}/health",
        "headers": lambda: {"X-User-ID": os.environ.get("NEXUS_AGENTSCOPE_USER_ID", "nexus")},
    },
    "task-mcp": {
        "cwd": ROOT / "services" / "nexus-api",
        "log_env": "TASK_MCP_LOG",
        "default_log": "/tmp/nexus-task-mcp.log",
        "command": task_mcp_command,
        "port": lambda: _env_port("NEXUS_TASK_MCP_PORT", 18202),
        "health": lambda port: f"http://127.0.0.1:{port}/health",
        "headers": lambda: {},
    },
    "nexus-api": {
        "cwd": ROOT / "services" / "nexus-api",
        "log_env": "NEXUS_API_LOG",
        "default_log": "/tmp/nexus-api.log",
        "command": nexus_api_command,
        "port": lambda: _env_port("NEXUS_API_PORT", 18120),
        "health": lambda port: f"http://127.0.0.1:{port}/api/health",
        "headers": lambda: {},
    },
}


def stop_service(name: str, *, quiet_absent: bool = False) -> int:
    processes = load_processes()
    info = processes.get(name)
    if not info:
        if not quiet_absent:
            print(f"{name} is not running.")
        return 0
    outcome = procman.release_record(info, label=name)
    if outcome == "keep":
        save_processes(processes)
        print(f"{name} did not stop completely.")
        return 1
    processes.pop(name, None)
    if processes:
        save_processes(processes)
    else:
        path = state_path()
        if path.exists():
            path.unlink()
    print(f"{name} stopped.")
    return 0


def start_service(name: str) -> int:
    stopped = stop_service(name, quiet_absent=True)
    if stopped != 0:
        return stopped
    spec = SERVICES[name]
    command_factory: Callable[[], list[str]] = spec["command"]
    try:
        command = command_factory()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    port = int(spec["port"]())
    log_path = Path(os.environ.get(spec["log_env"], spec["default_log"]))
    info = procman.spawn_process(command, spec["cwd"], log_path, port=port)
    processes = load_processes()
    processes[name] = info
    save_processes(processes)
    headers = spec["headers"]()
    url = spec["health"](port)
    try:
        procman.wait_http(name, url, headers=headers or None)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        if procman.release_record(info, label=name) == "keep":
            print(f"{name} did not stop completely.")
            return 1
        processes = load_processes()
        processes.pop(name, None)
        if processes:
            save_processes(processes)
        elif state_path().exists():
            state_path().unlink()
        return 1
    print(f"{name} ready; log {log_path}")
    return 0


def status_service(name: str) -> int:
    info = load_processes().get(name)
    if not info:
        print(f"{name} is not running.")
        return 1
    verdict = procman.identify_process(info, int(info["pid"]))
    if verdict == "ours":
        print(f"{name} running pid {info['pid']} port {info.get('port', '-')}")
        return 0
    print(f"{name} is not running.")
    return 1


def detach(name: str, command: list[str], log_path: Path) -> int:
    if stop_service(name, quiet_absent=True) != 0:
        return 1
    log_path.parent.mkdir(parents=True, exist_ok=True)
    info = procman.spawn_process(command, ROOT, log_path)
    processes = load_processes()
    processes[name] = info
    save_processes(processes)
    print(f"[DETACH] {name} PID={info['pid']} create_time={info.get('create_time')}")
    return 0 if info.get("create_time") else 1


def parse_headers(values: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition(":")
        if not sep or not key.strip():
            raise SystemExit(f"header must look like Name: value, got {item!r}")
        headers[key.strip()] = value.strip()
    return headers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start and stop Nexus service processes")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in SERVICES:
        service = sub.add_parser(name)
        service.add_argument("action", nargs="?", default="start", choices=("start", "stop", "status"))
    waiter = sub.add_parser("wait-http")
    waiter.add_argument("url")
    waiter.add_argument("--timeout", type=float, default=60)
    waiter.add_argument("--header", action="append", default=[])
    detached = sub.add_parser("detach")
    detached.add_argument("name")
    detached.add_argument("--log", required=True)
    detached.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "wait-http":
        try:
            procman.wait_http("check", args.url, timeout=args.timeout, headers=parse_headers(args.header))
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    if args.command == "detach":
        command = list(args.argv)
        if command[:1] == ["--"]:
            command = command[1:]
        if not command:
            print("detach requires a command", file=sys.stderr)
            return 2
        return detach(args.name, command, Path(args.log))
    action = args.action
    if action == "start":
        return start_service(args.command)
    if action == "stop":
        return stop_service(args.command)
    return status_service(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
