"""Local command/SFTP equivalent for server-deployment unit tests; not Linux/SSH E2E."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace


class LocalTarget:
    def __init__(self, root: Path, *, workspace: Path | None = None, action="deploy", env=None):
        self.environment = SimpleNamespace(id=71, workspace_root=str(workspace or root / "workspace"))
        self.operation_id, self.action, self.ssh = "test", action, self
        self.commands = []
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PIP_", "NPM_CONFIG_"))}
        self.env["HOME"] = str(root)
        self.env.update(env or {})

    def _agent_interpreter(self):
        return sys.executable

    async def _ensure_safe(self):
        return None

    async def read_file(self, path):
        path = Path(path)
        return path.read_bytes() if path.is_file() else None

    async def write_file(self, path, data, *, mode=0o600):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)

    async def _write_atomic_if_changed(self, path, data, mode, *, backup=False):
        path = Path(path)
        if path.exists() and path.read_bytes() == data:
            return False
        if backup and path.exists():
            copy = path.with_name(path.name + ".nexus-backup-" + self.operation_id)
            copy.write_bytes(path.read_bytes())
            copy.chmod(0o600)
        await self.write_file(path, data, mode=mode)
        return True

    async def _command(self, command, timeout=30):
        self.commands.append(command)
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            env=self.env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise RuntimeError(err.decode() or out.decode() or str(process.returncode))
        return out.decode().strip()
