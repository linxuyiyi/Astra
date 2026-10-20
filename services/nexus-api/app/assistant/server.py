"""平台助手专用 OpenCode Server 生命周期。

该进程以 Astra 项目根目录作为能力加载上下文启动，直接使用项目根的
``opencode.json`` 与 ``.opencode/``；不创建、不初始化任何助手专属
Workspace。它把当前 Python 虚拟环境的可执行目录注入 PATH，使 OpenCode
在 macOS、Linux 和 Windows 上都能找到 ``astra-assistant-mcp`` 入口。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

from app.config.settings import PROJECT_ROOT, Settings, get_settings
from app.executors.process import local_command
from app.runtime.opencode_server import OpenCodeServerManager


class AssistantOpenCodeServerManager(OpenCodeServerManager):
    """Describe, start and probe the isolated platform-assistant server."""

    def __init__(self, settings: Settings | None = None, *, binary: str | None = None):
        super().__init__(settings, binary=binary)

    @property
    def base_url(self) -> str:
        return self.settings.resolved_assistant_opencode_url

    @property
    def workspace(self) -> Path:
        """Astra project root: the assistant's capability-loading context only.

        The assistant never owns a workspace and never mutates this tree; write
        capabilities stay denied at the agent level.
        """

        return PROJECT_ROOT

    @property
    def executable_directory(self) -> Path:
        """Return the active environment's scripts directory without resolving venv links."""

        return Path(sys.executable).parent

    @property
    def mcp_entrypoint(self) -> Path:
        name = "astra-assistant-mcp.exe" if os.name == "nt" else "astra-assistant-mcp"
        return self.executable_directory / name

    def review_config(self) -> dict:
        """启动配置只带复盘 MCP。不读取根目录 opencode.json。"""

        return {
            "mcp": {
                "nexus-review": {
                    "type": "local",
                    "command": ["astra-assistant-mcp"],
                    "enabled": True,
                }
            }
        }

    def write_review_config(self) -> Path:
        directory = self.settings.resolved_data_dir
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "review-opencode.json"
        path.write_text(json.dumps(self.review_config()), encoding="utf-8")
        return path

    def command(self) -> list[str]:
        return local_command(
            self.binary,
            [
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.settings.assistant_opencode_port),
            ],
        )

    def process_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        # ``sys.executable`` is commonly a symlink inside Unix virtualenvs.
        # Resolving it points at the base Python installation, where the MCP
        # console script does not exist. Keep the virtualenv's own bin/Scripts
        # directory at the front of PATH on every platform.
        executable_dir = str(self.executable_directory)
        current_path = environment.get("PATH", "")
        environment["PATH"] = os.pathsep.join(item for item in (executable_dir, current_path) if item)
        # Marks this server as the assistant runtime so project-level plugins can
        # keep developer-only behaviour (e.g. the Superpowers bootstrap) out of
        # user-facing assistant sessions.
        environment["ASTRA_RUNTIME_ROLE"] = "review-executor"
        environment["OPENCODE_CONFIG"] = str(self.write_review_config())
        environment["OPENCODE_SERVER_USERNAME"] = self.settings.assistant_opencode_username
        if self.settings.assistant_opencode_password:
            environment["OPENCODE_SERVER_PASSWORD"] = self.settings.assistant_opencode_password
        else:
            # Do not accidentally reuse credentials intended for the execution server.
            environment.pop("OPENCODE_SERVER_PASSWORD", None)
        return environment

    @property
    def auth(self) -> httpx.BasicAuth | None:
        if not self.settings.assistant_opencode_password:
            return None
        return httpx.BasicAuth(
            self.settings.assistant_opencode_username,
            self.settings.assistant_opencode_password,
        )


def main() -> int:
    manager = AssistantOpenCodeServerManager(get_settings())
    if manager.settings.assistant_opencode_url:
        print(
            "ASTRA_ASSISTANT_OPENCODE_URL 已配置，不启动本地助手 OpenCode Server。",
            file=sys.stderr,
        )
        return 0

    command = manager.command()
    environment = manager.process_environment()
    os.chdir(manager.workspace)
    if os.name != "nt":
        os.execvpe(command[0], command, environment)
    process = subprocess.run(command, env=environment, check=False)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
