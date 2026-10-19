from __future__ import annotations

import platform
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import DeployConfig
from .runner import Runner


@dataclass
class PortListeners:
    pids: list[int]
    listening: bool
    error: str = ""


@dataclass
class PortStatus:
    port: int
    pids: list[int]
    free: bool
    owned: bool
    unit: str | None
    detail: str


class PortInspector:
    def __init__(self, runner: Runner) -> None:
        self.runner = runner

    def probe(self, port: int) -> PortListeners:
        if platform.system() == "Linux":
            result = self.runner.run(["ss", "-ltnpH", f"( sport = :{port} )"])
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip() or f"exit={result.returncode}"
                return PortListeners([], False, f"ss 检查失败：{detail}")
            lines = [line for line in result.stdout.splitlines() if line.strip()]
            pids = sorted({int(value) for value in re.findall(r"pid=(\d+)", result.stdout)})
            if lines and not pids:
                return PortListeners([], True, "监听存在但 ss 未给出 PID；请以 root 运行")
            return PortListeners(pids, bool(lines) or bool(pids))
        result = self.runner.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
        pids: list[int] = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        if result.returncode not in {0, 1}:
            detail = (result.stderr or result.stdout).strip() or f"exit={result.returncode}"
            return PortListeners([], False, f"lsof 检查失败：{detail}")
        if result.returncode == 0 and not pids:
            return PortListeners([], True, "监听存在但 lsof 未给出 PID")
        return PortListeners(sorted(set(pids)), bool(pids))

    def listening_pids(self, port: int) -> list[int]:
        return self.probe(port).pids

    @staticmethod
    def port_accepts_connections(port: int) -> bool:
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    return True
            except OSError:
                continue
        return False

    def cgroup_for(self, pid: int) -> str:
        path = Path(f"/proc/{pid}/cgroup")
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def inspect(
        self,
        port: int,
        *,
        unit: str | None,
        main_pid: int = 0,
        control_group: str = "",
    ) -> PortStatus:
        probed = self.probe(port)
        pids = probed.pids
        if probed.error:
            return PortStatus(port, pids, False, False, unit, probed.error)
        if not pids:
            if probed.listening:
                return PortStatus(
                    port,
                    pids,
                    False,
                    False,
                    unit,
                    "端口正在监听，但无法确认监听 PID；请以 root 运行预检",
                )
            if unit and main_pid:
                return PortStatus(port, pids, True, True, unit, "端口未监听，服务可能尚未启动")
            return PortStatus(port, pids, True, True, unit, "端口空闲")
        if not unit:
            return PortStatus(port, pids, False, False, None, "端口被未知进程占用")
        for pid in pids:
            if main_pid and pid == main_pid:
                continue
            cgroup = self.cgroup_for(pid)
            if control_group and control_group in cgroup:
                continue
            if f"{unit}.service" in cgroup:
                continue
            return PortStatus(
                port,
                pids,
                False,
                False,
                unit,
                f"监听 PID {pid} 不属于 {unit}.service",
            )
        return PortStatus(port, pids, False, True, unit, f"由 {unit}.service 监听")


class OwnershipChecker:
    """Check every port against the service unit that is allowed to own it."""

    def __init__(
        self,
        config: DeployConfig,
        inspector: PortInspector,
        *,
        main_pid: Callable[[str], int] | None = None,
        control_group: Callable[[str], str] | None = None,
    ) -> None:
        self.config = config
        self.inspector = inspector
        self._main_pid = main_pid or (lambda unit: 0)
        self._control_group = control_group or (lambda unit: "")

    def check(self, ports: dict[str, tuple[int, str]]) -> list[PortStatus]:
        statuses = []
        for _name, (port, unit) in ports.items():
            statuses.append(
                self.inspector.inspect(
                    port,
                    unit=unit,
                    main_pid=self._main_pid(unit),
                    control_group=self._control_group(unit),
                )
            )
        return statuses
