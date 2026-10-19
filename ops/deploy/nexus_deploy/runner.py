from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: int = 1800,
    ) -> CommandResult:
        rendered = [str(part) for part in command]
        try:
            proc = subprocess.run(
                rendered,
                cwd=str(cwd) if cwd else None,
                env=dict(env) if env is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(rendered, 127, "", f"command not found: {rendered[0]}")
        except subprocess.TimeoutExpired:
            return CommandResult(rendered, 124, "", f"timeout after {timeout}s")
        return CommandResult(rendered, proc.returncode, proc.stdout or "", proc.stderr or "")


def require_success(result: CommandResult, title: str) -> CommandResult:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = detail[-1] if detail else f"exit={result.returncode}"
        raise RuntimeError(f"{title} 失败：{tail}")
    return result
