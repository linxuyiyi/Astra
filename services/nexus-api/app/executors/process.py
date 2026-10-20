import asyncio
import os
import shutil
import subprocess
from pathlib import Path


def _strip_wrapping_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def resolve_executable(value: str) -> str | None:
    """Resolve a command name or an absolute executable path on the current OS."""

    command = _strip_wrapping_quotes(value)
    resolved = shutil.which(command)
    if resolved:
        return str(Path(resolved).resolve())

    candidate = Path(os.path.expandvars(command)).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return None


def local_command(executable: str, arguments: list[str]) -> list[str]:
    """Build argv without shell concatenation; adapt Windows batch launchers explicitly."""

    resolved = resolve_executable(executable)
    if not resolved:
        raise FileNotFoundError(f"未找到 Agent 可执行文件：{executable}")
    argv = [resolved, *arguments]
    if os.name == "nt" and Path(resolved).suffix.lower() in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe") or "cmd.exe"
        return windows_batch_command(resolved, arguments, comspec)
    return argv


def windows_batch_command(executable: str, arguments: list[str], comspec: str = "cmd.exe") -> list[str]:
    """Let subprocess.list2cmdline quote the argv consumed by cmd.exe.

    /s changes cmd.exe quote stripping rules and breaks quoted batch paths on
    some Windows installations. Passing the batch path and every argument as a
    separate argv item keeps spaces and non-ASCII paths intact.
    """

    return [comspec, "/d", "/c", executable, *arguments]


def process_group_options() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def terminate_process(process: asyncio.subprocess.Process, timeout: int = 10) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if taskkill:
            killer = await asyncio.create_subprocess_exec(
                taskkill,
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            process.terminate()
    else:
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
