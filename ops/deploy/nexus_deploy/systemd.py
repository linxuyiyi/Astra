from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

from .config import DeployConfig
from .errors import ServiceError
from .records import atomic_write
from .runner import Runner
from .services import ServiceCatalog, ServiceSpec


def _quote(argument: str) -> str:
    return shlex.quote(argument.replace("%", "%%"))


def render_unit(config: DeployConfig, service: ServiceSpec) -> str:
    log_path = config.log_dir / f"{service.unit}.log"
    exec_start = " ".join(_quote(part) for part in service.command)
    return f"""[Unit]
Description=Nexus {service.description}
After=network-online.target redis-server.service
Wants=network-online.target

[Service]
Type=simple
User={config.run_user}
Group={config.run_group}
WorkingDirectory={service.cwd}
EnvironmentFile={config.user_env_file}
EnvironmentFile={config.secrets_file}
EnvironmentFile={config.runtime_env_file}
EnvironmentFile=-{config.chat_model_file}
EnvironmentFile=-{config.optional_env_file}
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
TimeoutStartSec=90
TimeoutStopSec=30
KillMode=mixed
SyslogIdentifier={service.unit}
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=multi-user.target
"""


class SystemdManager:
    def __init__(
        self,
        config: DeployConfig,
        runner: Runner,
        *,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.runner = runner
        self.log = log

    def write_units(self, catalog: ServiceCatalog) -> list[Path]:
        self.config.unit_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for service in catalog.services:
            if not service.unit:
                continue
            path = self.config.unit_dir / f"{service.unit}.service"
            atomic_write(path, render_unit(self.config, service), mode=0o644)
            written.append(path)
        return written

    def daemon_reload(self) -> None:
        self._systemctl("daemon-reload")

    def enable(self, units: tuple[str, ...]) -> None:
        self._systemctl("enable", *units)

    def restart(self, units: tuple[str, ...]) -> None:
        self._systemctl("restart", *units)

    def stop(self, units: tuple[str, ...]) -> None:
        self._systemctl("stop", *units)

    def is_active(self, unit: str) -> bool:
        result = self.runner.run(["systemctl", "is-active", "--quiet", f"{unit}.service"])
        return result.returncode == 0

    def main_pid(self, unit: str) -> int:
        result = self.runner.run(
            ["systemctl", "show", "-p", "MainPID", "--value", f"{unit}.service"]
        )
        try:
            return int(result.stdout.strip() or "0")
        except ValueError:
            return 0

    def control_group(self, unit: str) -> str:
        result = self.runner.run(
            ["systemctl", "show", "-p", "ControlGroup", "--value", f"{unit}.service"]
        )
        return result.stdout.strip()

    def status(self, units: tuple[str, ...]) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for unit in units:
            result[unit] = {
                "active": self.is_active(unit),
                "main_pid": self.main_pid(unit),
            }
        return result

    def _systemctl(self, *args: str) -> None:
        if not Path("/run/systemd/system").exists() and not self.config.unit_dir.exists():
            raise ServiceError("systemd 不可用；生产部署需要 systemd")
        command = ["systemctl", *args]
        result = self.runner.run(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            tail = detail[-1] if detail else f"exit={result.returncode}"
            raise ServiceError(f"systemctl {' '.join(args)} 失败：{tail}")
