from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass

import httpx

from app.config.settings import Settings, get_settings
from app.executors.process import local_command


@dataclass(slots=True)
class OpenCodeServerHealth:
    healthy: bool
    version: str | None = None
    detail: str = ""


class OpenCodeServerManager:
    """Describe, start and probe the one shared OpenCode Server process."""

    def __init__(self, settings: Settings | None = None, *, binary: str | None = None):
        self.settings = settings or get_settings()
        self.binary = binary or self.settings.opencode_binary

    @property
    def base_url(self) -> str:
        return self.settings.resolved_opencode_server_url

    def command(self) -> list[str]:
        return local_command(
            self.binary,
            [
                "serve",
                "--hostname",
                self.settings.opencode_server_hostname,
                "--port",
                str(self.settings.opencode_server_port),
            ],
        )

    def process_environment(self) -> dict[str, str]:
        from app.runtime.opencode_model import gateway_environment

        environment = gateway_environment(self.settings.opencode_model, dict(os.environ))
        if self.settings.opencode_server_username:
            environment["OPENCODE_SERVER_USERNAME"] = self.settings.opencode_server_username
        if self.settings.opencode_server_password:
            environment["OPENCODE_SERVER_PASSWORD"] = self.settings.opencode_server_password
        return environment

    @property
    def auth(self) -> httpx.BasicAuth | None:
        if not self.settings.opencode_server_password:
            return None
        return httpx.BasicAuth(
            self.settings.opencode_server_username,
            self.settings.opencode_server_password,
        )

    async def health(self, *, timeout: float = 3) -> OpenCodeServerHealth:
        try:
            async with httpx.AsyncClient(auth=self.auth, timeout=timeout) as client:
                response = await client.get(f"{self.base_url}/global/health")
                response.raise_for_status()
                payload = response.json()
            version = str(payload.get("version") or "") or None
            version_error = self.version_error(version)
            if version_error:
                return OpenCodeServerHealth(False, version=version, detail=version_error)
            return OpenCodeServerHealth(
                healthy=payload.get("healthy") is True,
                version=version,
                detail=json.dumps(payload, ensure_ascii=False),
            )
        except (httpx.HTTPError, ValueError) as exc:
            return OpenCodeServerHealth(False, detail=str(exc))

    async def validate_gateway_configuration(self) -> None:
        from app.runtime.opencode_model import validate_gateway_config

        async with httpx.AsyncClient(auth=self.auth, timeout=3) as client:
            response = await client.get(f"{self.base_url}/config")
            response.raise_for_status()
            config = response.json()
        validate_gateway_config(config, dict(os.environ))

    async def wait_until_ready(self) -> OpenCodeServerHealth:
        deadline = time.monotonic() + self.settings.opencode_server_start_timeout_seconds
        last = OpenCodeServerHealth(False, detail="尚未检查")
        while time.monotonic() < deadline:
            last = await self.health()
            if last.healthy:
                await self.validate_gateway_configuration()
                return last
            await asyncio.sleep(0.2)
        raise TimeoutError(f"OpenCode Server 未就绪: {last.detail}")

    def version_error(self, version: str | None) -> str | None:
        """Compatibility gate shared by the manager and every client.

        Returns a human-readable reason when the server version is unsupported,
        or ``None`` when the check passes or is explicitly disabled. The
        disable switch lives in settings, never in a magic minimum version.
        """
        if not self.settings.opencode_server_version_check_enabled:
            return None
        expected = self.settings.opencode_server_version.strip().removeprefix("v")
        if not version or not str(version).strip():
            return f"OpenCode 未报告版本；唯一支持版本为 {expected}"
        normalized = str(version).strip().removeprefix("v")
        if normalized != expected:
            return f"OpenCode {version} 不是唯一支持版本 {expected}"
        return None


def main() -> int:
    manager = OpenCodeServerManager()
    command = manager.command()
    environment = manager.process_environment()
    if os.name != "nt":
        os.execvpe(command[0], command, environment)
    process = subprocess.run(command, env=environment, check=False)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
