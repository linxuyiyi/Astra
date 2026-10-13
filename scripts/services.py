#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Nexus 服务控制脚本。

统一管理两个独立进程：官方 AgentScope Agent Service（``agentscope``）与
Nexus 业务后端（``nexus-backend``）。运行方式（务必在已同步的项目环境中）：

    uv run python scripts/services.py start agentscope
    uv run python scripts/services.py start nexus-backend
    uv run python scripts/services.py status agentscope
    uv run python scripts/services.py stop nexus-backend

只操作由本脚本启动的进程：每次 start 生成随机实例标识（nonce），并把
pid+nonce+解释器+监听地址写入 JSON PID 记录，同时把 nonce 作为
``--instance-id`` 传给子进程；健康接口返回该 nonce。stop/status 会用
``ps`` 精确校验（解释器、模块、nonce 三者一致）并比对健康接口的 nonce，
拒绝接管或误杀非本实例进程。同 data_dir 的 start/stop 用小型文件锁互斥，
防止双 start 覆盖。PID 与日志放在被忽略的 data/ 下。

两个服务共用同一套 nonce/锁/归属控制流程（:class:`ServiceSpec`），不复制
第二套启停逻辑。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 允许在未安装包时也能直接运行（例如 uv run python scripts/services.py）。
sys.path.insert(0, str(ROOT / "agent-service"))
sys.path.insert(0, str(ROOT / "backend"))

from nexus_agent_service.config import load_config as load_agentscope_config  # noqa: E402
from nexus_backend.config import load_config as load_backend_config  # noqa: E402

INSTANCE_ID_FLAG = "--instance-id"
START_TIMEOUT_SECONDS = 60.0
STOP_TIMEOUT_SECONDS = 20.0
LOCK_TIMEOUT_SECONDS = 30.0

# 服务仅回环通信：显式禁用系统代理，避免代理把不可达伪装成可达。
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass(frozen=True)
class ServiceSpec:
    """一个受控服务的启停与归属描述。"""

    name: str
    module: str
    data_dir: Path
    host: str
    port: int
    internal_user_id: str
    health_path: str
    native_health_path: str | None
    pid_filename: str
    lock_filename: str
    log_filename: str

    @property
    def marker(self) -> str:
        return self.module

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def paths(self) -> "ServicePaths":
        run_dir = self.data_dir / "run"
        log_dir = self.data_dir / "logs"
        return ServicePaths(
            run_dir=run_dir,
            log_dir=log_dir,
            pid_file=run_dir / self.pid_filename,
            lock_file=run_dir / self.lock_filename,
            log_file=log_dir / self.log_filename,
        )


@dataclass(frozen=True)
class ServicePaths:
    """某服务的 PID/锁/日志文件位置。"""

    run_dir: Path
    log_dir: Path
    pid_file: Path
    lock_file: Path
    log_file: Path


SERVICE_NAMES = ("agentscope", "nexus-backend")


def build_spec(name: str) -> ServiceSpec:
    """只加载被操作服务的配置。

    绝不一次性解析两个服务：另一个服务配置非法时，不得阻塞本次对目标
    服务的 start/stop/status。
    """
    if name == "agentscope":
        return agentscope_spec(load_agentscope_config())
    if name == "nexus-backend":
        return backend_spec(load_backend_config())
    raise KeyError(name)


def agentscope_spec(config) -> ServiceSpec:
    return ServiceSpec(
        name="agentscope",
        module="nexus_agent_service",
        data_dir=config.data_dir,
        host=config.host,
        port=config.port,
        internal_user_id=config.internal_user_id,
        health_path="/nexus/health",
        native_health_path="/health",
        pid_filename="agentscope.pid",
        lock_filename="agentscope.lock",
        log_filename="agentscope.log",
    )


def backend_spec(config) -> ServiceSpec:
    return ServiceSpec(
        name="nexus-backend",
        module="nexus_backend",
        data_dir=config.data_dir,
        host=config.host,
        port=config.port,
        internal_user_id=config.internal_user_id,
        health_path="/api/health",
        native_health_path=None,
        pid_filename="nexus-backend.pid",
        lock_filename="nexus-backend.lock",
        log_filename="nexus-backend.log",
    )


@dataclass
class InstanceRecord:
    """本脚本启动实例的归属凭证。"""

    pid: int
    instance_id: str
    interpreter: str
    host: str
    port: int
    started_at: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def read_record(pid_file: Path) -> InstanceRecord | None:
    """读取 JSON PID 记录；缺失、空文件、旧纯整数格式均返回 None。"""
    try:
        raw = pid_file.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return InstanceRecord(
            pid=int(payload["pid"]),
            instance_id=str(payload["instance_id"]),
            interpreter=str(payload["interpreter"]),
            host=str(payload["host"]),
            port=int(payload["port"]),
            started_at=str(payload["started_at"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def process_command(pid: int) -> str:
    try:
        return subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def process_owned_by(
    pid: int,
    instance_id: str,
    interpreter: str,
    marker: str,
) -> bool:
    """精确校验 pid 的命令行属于本项目解释器 + 模块 + 指定 nonce。

    不做纯子串匹配：必须首个参数即同一解释器、模块 token 存在、且
    ``--instance-id`` 的值与记录一致。
    """
    command = process_command(pid)
    if not command:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False
    if os.path.realpath(argv[0]) != os.path.realpath(interpreter):
        return False
    if marker not in argv:
        return False
    if INSTANCE_ID_FLAG not in argv:
        return False
    index = argv.index(INSTANCE_ID_FLAG)
    if index + 1 >= len(argv) or argv[index + 1] != instance_id:
        return False
    return True


def record_is_owned(record: InstanceRecord, marker: str) -> bool:
    return is_alive(record.pid) and process_owned_by(
        record.pid,
        record.instance_id,
        record.interpreter,
        marker,
    )


@contextmanager
def service_lock(paths: ServicePaths, timeout: float = LOCK_TIMEOUT_SECONDS):
    """同 data_dir 的跨进程互斥锁，保护 start/stop 与 PID 读写。"""
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    with paths.lock_file.open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "另一个 start/stop 操作正在进行，请稍后重试。",
                    )
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def port_in_use(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def http_get(url: str, headers: dict | None = None, timeout: float = 2.0):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except (ValueError, OSError):
            body = {}
        return exc.code, body


def fetch_report(
    spec: ServiceSpec,
    expected_instance_id: str | None = None,
) -> dict | None:
    """读取主健康与（可选）原生健康；不可达或实例不匹配返回 None。"""
    try:
        primary_status, primary = http_get(
            f"{spec.base_url}{spec.health_path}",
        )
        native_status: int | None = None
        native: dict | None = None
        if spec.native_health_path is not None:
            native_status, native = http_get(
                f"{spec.base_url}{spec.native_health_path}",
                headers={"X-User-ID": spec.internal_user_id},
            )
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if (
        expected_instance_id is not None
        and primary.get("instance_id") != expected_instance_id
    ):
        return None
    return {
        "primary_status": primary_status,
        "primary": primary,
        "native_status": native_status,
        "native": native,
    }


def report_is_healthy(spec: ServiceSpec, report: dict | None) -> bool:
    """实例健康判据：同一 nonce 下 HTTP 200 且 ``ready=true``。

    两个服务都用 HTTP 状态编码真实健康语义：AgentScope ``/nexus/health``
    仅在 lifespan 就绪时 200；后端 ``/api/health`` 仅在上游连通且版本与
    初始化正常时 200。故此判据同时覆盖各自的服务健康语义。
    """
    if report is None or report.get("primary_status") != 200:
        return False
    primary = report.get("primary") or {}
    return primary.get("ready") is True


def wait_for_health(
    spec: ServiceSpec,
    timeout: float,
    process: subprocess.Popen | None,
    instance_id: str,
) -> dict | None:
    """轮询直到本进程就绪（``ready``）且 nonce 匹配，且子进程仍存活。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return None
        report = fetch_report(spec, instance_id)
        if (
            report is not None
            and report["primary"].get("ready") is True
        ):
            return report
        time.sleep(0.5)
    return None


def tail_log(paths: ServicePaths, lines: int = 40) -> None:
    try:
        content = paths.log_file.read_text(errors="replace").splitlines()
    except OSError:
        print("（无日志）")
        return
    print("---- 日志末尾 ----")
    for line in content[-lines:]:
        print(line)
    print("------------------")


def print_status(
    spec: ServiceSpec,
    paths: ServicePaths,
    record: InstanceRecord | None,
    report: dict | None,
) -> None:
    print(f"服务: {spec.name}")
    print(f"地址: {spec.base_url}")
    print(f"数据目录: {spec.data_dir}")
    print(f"日志: {paths.log_file}")
    if record is None:
        if paths.pid_file.exists():
            print("进程: PID 记录无法识别，已拒绝接管")
        else:
            print("进程: 未运行")
    elif not is_alive(record.pid):
        print(f"进程: 未运行（残留 PID {record.pid}）")
    elif not record_is_owned(record, spec.marker):
        print(f"进程: PID {record.pid} 存活但不是本实例，已拒绝接管")
    else:
        print(
            f"进程: 运行中（PID {record.pid}，实例 {record.instance_id[:8]}）",
        )
    if report is None:
        print("健康: 不可达或未就绪")
        return
    primary = report["primary"]
    if report["primary_status"] != 200 or not primary.get("ready"):
        print(
            f"健康: 未就绪（HTTP {report['primary_status']}）"
            f"{'：' + primary['reason'] if primary.get('reason') else ''}",
        )
        return
    if spec.name == "agentscope":
        print(
            "健康: 服务正常"
            f"，模型{'已配置' if primary.get('model_configured') else '未配置'}"
            f"（原生 HTTP {report['native_status']}）",
        )
        if not primary.get("model_configured") and primary.get("reason"):
            print(f"模型说明: {primary['reason']}")
    else:
        upstream = primary.get("agentscope", {})
        print(
            f"健康: {primary.get('status')}"
            f"，上游 AgentScope "
            f"{'已连接' if upstream.get('reachable') else '不可用'}"
            f"（版本 {upstream.get('version') or '未知'}）",
        )
        if primary.get("reason"):
            print(f"说明: {primary['reason']}")


def command_start(spec: ServiceSpec, paths: ServicePaths) -> int:
    with service_lock(paths):
        existing = read_record(paths.pid_file)
        if existing is None and paths.pid_file.exists():
            print(
                "PID 记录无法识别（非本脚本格式），拒绝启动以避免冲突。",
                file=sys.stderr,
            )
            return 1
        if existing is not None:
            if record_is_owned(existing, spec.marker):
                report = fetch_report(spec, existing.instance_id)
                healthy = report_is_healthy(spec, report)
                if healthy:
                    print(f"已在运行（PID {existing.pid}），健康正常，无需重复启动。")
                else:
                    # 已属于本实例但不健康：只如实报告，绝不自动重启。
                    print(
                        f"本实例进程已存在（PID {existing.pid}）但健康未就绪，"
                        "不自动重启。",
                        file=sys.stderr,
                    )
                print_status(spec, paths, existing, report)
                return 0 if healthy else 1
            if is_alive(existing.pid):
                print(
                    f"PID {existing.pid} 存活但不属于本服务实例，"
                    "拒绝启动以避免冲突。",
                    file=sys.stderr,
                )
                return 1
            # 残留记录：进程已不存在，清理后继续启动。
            paths.pid_file.unlink(missing_ok=True)

        if port_in_use(spec.host, spec.port):
            print(
                f"端口 {spec.host}:{spec.port} 已被占用，启动失败。"
                "请先停止占用方或修改监听端口配置。",
                file=sys.stderr,
            )
            return 1

        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        instance_id = uuid.uuid4().hex
        command = [
            sys.executable,
            "-m",
            spec.module,
            INSTANCE_ID_FLAG,
            instance_id,
        ]
        with paths.log_file.open("ab") as log_handle:
            log_handle.write(
                f"\n=== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"(instance {instance_id[:8]}) ===\n".encode(),
            )
            log_handle.flush()
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        record = InstanceRecord(
            pid=process.pid,
            instance_id=instance_id,
            interpreter=sys.executable,
            host=spec.host,
            port=spec.port,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        paths.pid_file.write_text(record.to_json())

        report = wait_for_health(
            spec,
            START_TIMEOUT_SECONDS,
            process,
            instance_id,
        )
        if report is None:
            print("启动后健康检查超时，正在停止该进程。", file=sys.stderr)
            _terminate(record, spec.marker)
            if process.poll() is None:
                try:
                    process.wait(timeout=STOP_TIMEOUT_SECONDS)
                    stopped = True
                except subprocess.TimeoutExpired:
                    stopped = False
            else:
                stopped = True
            if stopped:
                paths.pid_file.unlink(missing_ok=True)
            else:
                print(
                    f"无法确认 PID {record.pid} 退出，保留 PID 记录。",
                    file=sys.stderr,
                )
            tail_log(paths)
            return 1

        print(f"启动成功（PID {record.pid}）。")
        print_status(spec, paths, record, report)
        return 0


def _terminate(record: InstanceRecord, marker: str) -> bool:
    """发送 TERM/KILL 并在每次信号前重新校验归属；返回是否确认退出。"""
    pid = record.pid
    if not is_alive(pid):
        return True
    if not record_is_owned(record, marker):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not is_alive(pid)

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        if not record_is_owned(record, marker):
            return False
        time.sleep(0.3)

    if not record_is_owned(record, marker):
        return False
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return not is_alive(pid)

    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.3)
    return not is_alive(pid)


def command_stop(spec: ServiceSpec, paths: ServicePaths) -> int:
    with service_lock(paths):
        record = read_record(paths.pid_file)
        if record is None:
            if paths.pid_file.exists():
                print(
                    "PID 记录无法识别（非本脚本格式），拒绝停止以免误杀。",
                    file=sys.stderr,
                )
                return 1
            print("未找到 PID 文件，服务未运行。")
            return 0
        if not is_alive(record.pid):
            print(f"PID {record.pid} 已不存在，清理残留 PID 文件。")
            paths.pid_file.unlink(missing_ok=True)
            return 0
        if not record_is_owned(record, spec.marker):
            print(
                f"PID {record.pid} 不是本实例，拒绝停止以免误杀。",
                file=sys.stderr,
            )
            return 1
        if not _terminate(record, spec.marker):
            print(
                f"PID {record.pid} 未能确认退出，保留 PID 文件。",
                file=sys.stderr,
            )
            return 1
        paths.pid_file.unlink(missing_ok=True)
        print(f"已停止（PID {record.pid}）。")
        return 0


def command_status(spec: ServiceSpec, paths: ServicePaths) -> int:
    record = read_record(paths.pid_file)
    report = None
    owned = record is not None and record_is_owned(record, spec.marker)
    if owned and record is not None:
        report = fetch_report(spec, record.instance_id)
    print_status(spec, paths, record, report)
    # 与 start 用同一判据：同一 nonce 下 HTTP 200 且 ready。
    return 0 if owned and report_is_healthy(spec, report) else 1


COMMANDS = {
    "start": command_start,
    "stop": command_stop,
    "status": command_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nexus 服务控制脚本")
    parser.add_argument("action", choices=sorted(COMMANDS))
    parser.add_argument(
        "service",
        nargs="?",
        default="agentscope",
        help="要操作的服务（agentscope / nexus-backend）",
    )
    args = parser.parse_args(argv)

    if args.service not in SERVICE_NAMES:
        print(
            f"未知服务 {args.service!r}；可选：{', '.join(SERVICE_NAMES)}",
            file=sys.stderr,
        )
        return 1
    try:
        spec = build_spec(args.service)
    except ValueError as exc:
        print(
            f"无法加载 {args.service} 配置：{exc}",
            file=sys.stderr,
        )
        return 1
    paths = spec.paths()
    try:
        return COMMANDS[args.action](spec, paths)
    except TimeoutError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
