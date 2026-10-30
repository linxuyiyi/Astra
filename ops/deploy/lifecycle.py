"""平台唯一部署进程清单，复用既有 procman，不引入守护框架。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from configuration import DeploymentError, Dialog, atomic_write, parse_env


def procman_for(root):
    spec = importlib.util.spec_from_file_location(
        "nexus_deploy_procman", root / "ops/dev/procman.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def services(root, state, env):
    backend = root / "services/nexus-api"
    py = str(backend / ".venv/bin/python")
    agentpy = str(root / "services/agent-service/.venv-agentscope/bin/python")
    instance = hashlib.sha256(str(state).encode()).hexdigest()[:8]

    def http(name, command, port, health, cwd=backend, headers=None):
        return {
            "name": name,
            "command": command,
            "cwd": str(cwd),
            "port": int(port),
            "health": health,
            "headers": headers or {},
        }

    def uvicorn(module, port):
        return [
            py,
            "-m",
            "uvicorn",
            module,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--timeout-graceful-shutdown",
            "30",
        ]

    api, gateway = env["NEXUS_API_PORT"], env["ASTRA_SERVICE_GATEWAY_PORT"]
    result = [
        http(
            "agent-service",
            [agentpy, str(root / "services/agent-service/agent_service.py")],
            env["AGENTSCOPE_PORT"],
            "/health",
            root / "services/agent-service",
            {"X-User-ID": "nexus", "X-Nexus-Internal-Key": env["NEXUS_INTERNAL_KEY"]},
        ),
        http(
            "gateway", uvicorn("app.gateway.server:app", gateway), gateway, "/healthz"
        ),
        http("nexus-api", uvicorn("nexus_api.service:app", api), api, "/api/health"),
        http(
            "library-mcp", [py, "-m", "app.mcp_server"], env["NEXUS_MCP_PORT"], "/mcp"
        ),
        http(
            "task-mcp",
            [py, "-c", "from app.task_mcp import main;main()"],
            env["NEXUS_TASK_MCP_PORT"],
            "/health",
        ),
        http(
            "schedule-mcp",
            [py, "-c", "from nexus_api.schedule_mcp import main;main()"],
            env["NEXUS_SCHEDULE_MCP_PORT"],
            "/health",
        ),
    ]
    celery = [py, "-m", "celery", "-A", "app.runtime.celery_app:celery"]
    for queue in ("nexus-control", "agent", "nexus-review"):
        node = f"{queue}-{instance}@{socket.gethostname()}"
        result.append(
            {
                "name": queue,
                "cwd": str(backend),
                "command": celery
                + [
                    "worker",
                    "--loglevel=INFO",
                    "--pool=solo",
                    "--queues=" + queue,
                    "--hostname=" + node,
                ],
                "node": node,
            }
        )
    result.append(
        {
            "name": "runtime-beat",
            "cwd": str(backend),
            "command": celery
            + [
                "beat",
                "--loglevel=INFO",
                "--schedule=" + str(state / "runtime/celerybeat-schedule"),
            ],
        }
    )
    result.append(
        {
            "name": "nginx",
            "cwd": str(state),
            "command": [
                env["NEXUS_DEPLOY_NGINX_BINARY"],
                "-p",
                str(state / "nginx") + "/",
                "-c",
                str(state / "nginx.conf"),
                "-g",
                "daemon off;",
            ],
            "port": int(env["NEXUS_DEPLOY_HTTPS_PORT"]),
        }
    )
    return result


class Manager:
    def __init__(self, root, state, env):
        self.root, self.state, self.env = root, state, env
        self.procman = procman_for(root)
        self.path = state / "processes.json"
        self.specs = services(root, state, env)
        if self.path.is_file():
            try:
                self.records = json.loads(self.path.read_text())
                if not isinstance(self.records, dict):
                    raise TypeError()
            except (ValueError, TypeError):
                raise DeploymentError(
                    "进程记录无效，拒绝接管；请人工核对现场"
                ) from None
        else:
            self.records = {}
            self.import_legacy_records()

    def import_legacy_records(self):
        checkout = Path(self.env.get("NEXUS_ROOT", self.root)).resolve()
        runtime = Path(self.env.get("ASTRA_RUNTIME_DIR", checkout / ".astra/run"))
        if not runtime.is_absolute():
            runtime = checkout / runtime
        paths = [
            self.state / "service-ctl.json",
            runtime / "astra-service.json",
            checkout / ".astra/run/astra-service.json",
        ]
        allowed = (checkout, self.state.resolve())
        names = {
            "agent": "agent-service",
            "runtime-control": "nexus-control",
            "runtime-agent": "agent",
            "runtime-review": "nexus-review",
        }
        imported = {}
        for path in dict.fromkeys(paths):
            if not path.is_file():
                continue
            try:
                entries = json.loads(path.read_text())["processes"]
                if not isinstance(entries, dict):
                    raise TypeError()
                for name, record in entries.items():
                    pid = int(record["pid"])
                    if not self.procman.leader_live(pid):
                        if record.get("pgid") == pid and self.procman.group_alive(pid):
                            raise DeploymentError(
                                "旧进程组仍存活但缺少首进程，需人工核对"
                            )
                        continue
                    cwd = Path(record["cwd"]).resolve()
                    if not any(
                        cwd == base or base in cwd.parents for base in allowed
                    ) or not self.owned(record):
                        raise DeploymentError("旧部署进程身份或目录不符，拒绝迁移接管")
                    target_name = names.get(name, name)
                    if target_name in imported and imported[target_name]["pid"] != pid:
                        raise DeploymentError("旧部署存在同名多进程，请先明确处理")
                    imported[target_name] = record
            except (ValueError, TypeError, KeyError):
                raise DeploymentError("旧部署进程记录无效，未发送停止信号") from None
        if imported:
            self.records = imported
            self.save()
            print("[部署] 已核对并迁移旧 Python 进程记录；旧记录保留作诊断")

    def save(self):
        atomic_write(self.path, json.dumps(self.records, indent=2) + "\n")

    def owned(self, record):
        pid = record.get("pid", 0)
        live = self.procman.read_process_identity(pid)
        return (
            pid > 1
            and live
            and record.get("pgid") == pid
            and live.get("pgid") == pid
            and live.get("cwd") == record.get("cwd")
            and self.procman.identify_process(record, pid) == "ours"
        )

    def preflight(self):
        import psutil

        for name, record in self.records.items():
            if self.procman.leader_live(record.get("pid", 0)) and not self.owned(
                record
            ):
                raise DeploymentError(f"{name} 进程身份不符，拒绝发送信号")
        expected_ports = {
            spec["port"]: spec["name"] for spec in self.specs if spec.get("port")
        }
        expected_ports[int(self.env["NEXUS_DEPLOY_WORKER_PORT"])] = "nginx"
        groups = {
            name: record.get("pgid")
            for name, record in self.records.items()
            if self.owned(record)
        }
        for connection in psutil.net_connections(kind="tcp"):
            if connection.status != psutil.CONN_LISTEN or not connection.laddr:
                continue
            name = expected_ports.get(connection.laddr.port)
            if not name:
                continue
            try:
                group = os.getpgid(connection.pid) if connection.pid else None
            except OSError:
                group = None
            if group is None or group != groups.get(name):
                raise DeploymentError(
                    f"{name} 端口 {connection.laddr.port} 被非受管进程占用；不会强杀或自动接管"
                )

    def stop(self):
        self.preflight()
        for name in reversed(list(self.records)):
            record = self.records[name]
            if self.procman.leader_live(record.get("pid", 0)) and not self.owned(
                record
            ):
                raise DeploymentError(f"{name} 身份变化，停止失败")
            if self.procman.release_record(record, label=name) != "drop":
                raise DeploymentError(f"{name} 未完全退出，部署停止")
            del self.records[name]
            self.save()

    def check_identity(self, spec):
        import psutil

        record = self.records.get(spec["name"], {})
        if not record or not self.owned(record):
            raise DeploymentError(f"{spec['name']} 不存活或身份不符")
        process = psutil.Process(record["pid"])
        if spec["name"] == "nginx":
            # Nginx 会重写 argv/environ 内存；核对二进制和启动参数，实际加载版本另从 HTTPS 响应验收。
            if Path(process.exe()).resolve() != Path(
                spec["command"][0]
            ).resolve() or " ".join(spec["command"]) not in " ".join(process.cmdline()):
                raise DeploymentError("Nginx 实际可执行文件或配置启动参数不符")
        elif (
            process.environ().get("NEXUS_DEPLOY_RELEASE")
            != self.env["NEXUS_DEPLOY_RELEASE"]
        ):
            raise DeploymentError(f"{spec['name']} 实际进程发布版本不符")
        if (
            record.get("release") != self.env["NEXUS_DEPLOY_RELEASE"]
            or record.get("command") != spec["command"]
        ):
            raise DeploymentError(f"{spec['name']} 启动清单不符")

    def wait_health(self, spec, timeout=120):
        if not spec.get("health"):
            return
        url = f"http://127.0.0.1:{spec['port']}" + spec["health"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_identity(spec)
            headers = dict(spec.get("headers", {}))
            data = None
            if spec["name"] == "library-mcp":
                headers.update(
                    Authorization="Bearer " + self.env["NEXUS_MCP_API_KEY"],
                    Accept="application/json, text/event-stream",
                    **{"Content-Type": "application/json"},
                )
                data = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "nexus-deploy", "version": "1.4.9"},
                        },
                    }
                ).encode()
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers, data=data), timeout=3
                ) as response:
                    body = response.read(262144)
                    if response.status == 200 and (
                        b'"result"' in body
                        if data
                        else (
                            json.loads(body).get("status") in ("ok", "healthy")
                            or json.loads(body).get("ok") is True
                        )
                    ):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        raise DeploymentError(f"{spec['name']} 健康验证失败，查看私有日志")

    def start(self):
        self.preflight()
        if self.records:
            raise DeploymentError("必须先完全停止旧进程才能启动新发布")
        for spec in self.specs:
            record = self.procman.spawn_process(
                spec["command"],
                Path(spec["cwd"]),
                self.state / "logs" / (spec["name"] + ".log"),
                env=self.env,
                port=spec.get("port"),
            )
            record["release"] = self.env["NEXUS_DEPLOY_RELEASE"]
            self.records[spec["name"]] = record
            self.save()
            self.wait_health(spec)
        self.health()

    def health(self):
        import subprocess

        for spec in self.specs:
            self.check_identity(spec)
            self.wait_health(spec)
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=self.env["NEXUS_DEPLOY_TLS_CERT"])
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        url = (
            "https://"
            + self.env["NEXUS_DEPLOY_PUBLIC_HOST"]
            + ":"
            + self.env["NEXUS_DEPLOY_HTTPS_PORT"]
            + "/api/health"
        )
        deadline = time.monotonic() + 20
        while True:
            try:
                with opener.open(url, timeout=3) as response:
                    if (
                        response.status == 200
                        and response.headers.get("X-Nexus-Release")
                        == self.env["NEXUS_DEPLOY_RELEASE"]
                    ):
                        break
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise DeploymentError("Nginx 实际加载版本或 HTTPS 健康检查失败")
            time.sleep(0.2)
        celery = [spec for spec in self.specs if spec.get("node")]
        command = [
            str(self.root / "services/nexus-api/.venv/bin/python"),
            "-m",
            "celery",
            "-A",
            "app.runtime.celery_app:celery",
            "inspect",
            "ping",
            "--json",
            "--timeout",
            "2",
            "--destination",
            ",".join(spec["node"] for spec in celery),
        ]
        deadline = time.monotonic() + 45
        while True:
            for spec in celery:
                self.check_identity(spec)
            check = subprocess.run(
                command,
                cwd=self.root / "services/nexus-api",
                env=self.env,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            atomic_write(
                self.state / "logs/celery-health.log", check.stdout + check.stderr
            )
            try:
                replies = json.loads(check.stdout)
                if check.returncode == 0 and all(
                    replies.get(spec["node"], {}).get("ok") == "pong" for spec in celery
                ):
                    return
            except (ValueError, AttributeError):
                pass
            if time.monotonic() >= deadline:
                raise DeploymentError(
                    "Celery 全部队列 ping 验收失败，查看私有 celery-health.log"
                )
            time.sleep(0.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preflight", "stop", "start", "health"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("NEXUS_", "ASTRA_", "AGENTSCOPE_"))
    }
    env.update(parse_env(args.state / "nexus.env", Dialog(False)))
    # Settings 只读同一运行环境，不吸收 checkout 中开发者配置。
    os.environ.clear()
    os.environ.update(env)
    try:
        getattr(Manager(args.root, args.state, env), args.action)()
    except (DeploymentError, OSError) as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
