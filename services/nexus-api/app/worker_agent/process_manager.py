"""无 systemd Linux 的受管进程；记录身份，拒绝按裸 PID 或名称停止进程。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from app.worker_agent.process_env import child_environ


def has_systemd() -> bool:
    try:
        return Path("/proc/1/comm").read_text().strip() == "systemd" and Path(
            "/run/systemd/system"
        ).is_dir()
    except OSError:
        return False


def process_identity(pid: int) -> dict:
    try:
        root = Path(f"/proc/{pid}")
        stat = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return {}
        return {
            "pid": pid,
            "starttime": stat[19],
            "cwd": os.readlink(root / "cwd"),
            "argv": (root / "cmdline").read_bytes().decode().split("\0")[:-1],
            "pgid": os.getpgid(pid),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError("无法核对受管进程身份，未停止进程") from exc


def _read_record(path: Path) -> dict | None:
    try:
        record = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError("进程记录不可读，未停止进程") from exc
    if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
        raise RuntimeError("进程记录无效，未停止进程")
    identity = record["identity"]
    if not isinstance(identity.get("pid"), int) or identity["pid"] <= 1:
        raise RuntimeError("进程 PID 无效，未停止进程")
    return record


def managed_running(path: Path) -> bool:
    record = _read_record(path)
    if record is None:
        return False
    expected = record["identity"]
    current = process_identity(expected["pid"])
    if not current:
        return False
    if current != expected or current["pgid"] != current["pid"]:
        raise RuntimeError("受管进程身份已变化，拒绝停止或覆盖记录")
    return True


def _stop(path: Path) -> None:
    if not managed_running(path):
        return
    record = _read_record(path)
    identity = record["identity"]
    pid = identity["pid"]
    if pid == os.getpid():
        raise RuntimeError("拒绝结束当前维护进程")
    # 紧邻信号再次核对，保留不确定记录；不升级为模糊匹配或 SIGKILL。
    if process_identity(pid) != identity:
        raise RuntimeError("受管进程身份已变化，未停止进程")
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not process_identity(pid):
            return
        time.sleep(0.1)
    raise RuntimeError("受管进程未在时限内退出，保留记录")


def manage_process(
    record_path: Path,
    *,
    command: list[str],
    workspace: str,
    log_path: Path,
    environ: dict[str, str],
    restart: bool = False,
    opencode: bool = False,
    gate: Callable[[], None] | None = None,
) -> None:
    record_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with record_path.with_suffix(".lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        running = managed_running(record_path)
        if running:
            record = _read_record(record_path)
            if record["identity"]["cwd"] != str(Path(workspace).resolve()):
                raise RuntimeError("受管进程工作区已变化，未停止进程")
        # 端口不是进程身份；已有监听必须属于记录中的受管进程组。
        port = None
        if opencode:
            from app.worker_agent.channel_switch import _listener_pid, _probe_listener

            port = int(command[command.index("--port") + 1])
            if _probe_listener(port):
                listener = _listener_pid(port)
                record = _read_record(record_path)
                if not running or listener is None or os.getpgid(listener) != record["identity"]["pid"]:
                    raise RuntimeError("OpenCode 端口不属于受管进程，拒绝启动或停止")
        if running and not restart:
            record = _read_record(record_path)
            if record.get("command") != command or record["identity"]["cwd"] != str(
                Path(workspace).resolve()
            ):
                raise RuntimeError("受管进程启动参数已变化，需要显式重启")
            return
        if running:
            if gate is not None:
                gate()
            _stop(record_path)
        if port is not None:
            deadline = time.monotonic() + 5
            while _probe_listener(port):
                if gate is not None:
                    gate()
                if time.monotonic() >= deadline:
                    raise RuntimeError("OpenCode 端口仍有监听，拒绝重复启动")
                time.sleep(0.1)
        if gate is not None:
            gate()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with log_path.open("ab") as output:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=child_environ(environ) if opencode else environ,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        # 采集 exec 后的实际参数；拒绝已退出的进程，再原子保存身份。
        time.sleep(0.1)
        identity = process_identity(process.pid)
        if process.poll() is not None or not identity:
            raise RuntimeError("受管进程启动后立即退出，请检查私有日志")
        temporary = record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"identity": identity, "command": command}) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, record_path)


def read_environment(path: Path) -> dict[str, str]:
    """环境文件为数据，不执行 shell；仅由管理员/部署器写入。"""

    result = dict(os.environ)
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key.isidentifier():
            raise RuntimeError("受管环境文件格式无效")
        result[key] = value
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["ensure", "restart", "status"])
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--log", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--opencode", action="store_true")
    raw = sys.argv[1:]
    separator = raw.index("--") if "--" in raw else len(raw)
    args = parser.parse_args(raw[:separator])
    if args.action == "status":
        print("running" if managed_running(args.record) else "stopped")
        return
    command = raw[separator + 1:]
    if not command or not args.workspace or not args.log or not args.env_file:
        parser.error("需要 command、workspace、log、env-file")
    manage_process(
        args.record, command=command, workspace=args.workspace, log_path=args.log,
        environ=read_environment(args.env_file), restart=args.action == "restart",
        opencode=args.opencode,
    )


if __name__ == "__main__":
    main()
