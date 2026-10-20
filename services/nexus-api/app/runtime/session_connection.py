"""Server transport and task directory binding; no execution state or message storage."""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from app.config.settings import get_settings
from app.database import SessionLocal
from app.domain import ExecutionMode
from app.executors.opencode_local_server import LocalOpenCodeServerExecutor
from app.executors.opencode_server import OpenCodeServerClient
from app.models import OpenCodeExecutionEnvironment, Task
from app.runtime.environment import environment_from_snapshot
from app.runtime.worker_channel import read_worker_channel
from app.runtime.worker_client import WorkerOpenCodeClient


async def session_tree(client, directory: Path, root: str) -> set[str]:
    """Return the native root Session plus all current descendants."""

    allowed, pending = {root}, [root]
    while pending:
        children = await client.children(directory, pending.pop())
        for child in children:
            child_id = child.get("id")
            if child_id and child_id not in allowed:
                allowed.add(child_id)
                pending.append(child_id)
    return allowed


async def resolve_environment(session, task: Task):
    snapshot = task.execution_environment_snapshot
    if not snapshot:
        raise ValueError("任务尚未绑定执行环境")
    environment = environment_from_snapshot(snapshot, task.employee_id)
    stored = await session.get(OpenCodeExecutionEnvironment, snapshot["environment_id"])
    if stored:
        environment.ssh_password = stored.ssh_password
    return environment


async def resolve_runtime_session_environment(session, binding):
    """Resolve credentials from a RuntimeSession's immutable environment binding."""

    snapshot = binding.environment
    environment = environment_from_snapshot(snapshot, binding.employee_id)
    stored = await session.get(OpenCodeExecutionEnvironment, snapshot.get("environment_id"))
    if stored:
        environment.ssh_password = stored.ssh_password
    return environment


async def _worker_channel(environment) -> str:
    environment_id = getattr(environment, "id", None)
    async with SessionLocal() as session:
        return await read_worker_channel(session, environment_id)


@asynccontextmanager
async def connect(environment, *, start: bool = False):
    settings = get_settings()
    if environment.mode == ExecutionMode.LOCAL:
        if start:
            await LocalOpenCodeServerExecutor(environment, settings).ensure_ready()
        yield OpenCodeServerClient(settings, base_url=f"http://127.0.0.1:{environment.opencode_server_port}")
        return
    if await _worker_channel(environment) == "worker":
        client = WorkerOpenCodeClient(settings, environment_id=environment.id)
        if start:
            await client.health()
        yield client
        return
    raise RuntimeError("远程环境尚未安装 Worker")


async def _local_git(args: list[str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    return (
        process.returncode,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def _git_common_dir(path: Path) -> Path | None:
    code, out, _ = await _local_git(["-C", str(path), "rev-parse", "--git-common-dir"])
    if code != 0 or not out:
        return None
    candidate = Path(out)
    if not candidate.is_absolute():
        candidate = path / candidate
    return candidate.resolve()


async def _assert_valid_task_worktree(main_repo: Path, target: Path) -> None:
    """Prove an existing directory is this repository's worktree for the task."""

    if target.is_symlink() or not target.is_dir():
        raise RuntimeError(f"任务工作区路径异常（非目录或符号链接）：{target}")
    code, top, _ = await _local_git(["-C", str(target), "rev-parse", "--show-toplevel"])
    if code != 0 or Path(top).resolve() != target.resolve():
        raise RuntimeError(f"任务工作区不是合法的 Git worktree：{target}")
    if await _git_common_dir(main_repo) != await _git_common_dir(target):
        raise RuntimeError(f"任务工作区不属于当前仓库：{target}")
    code, listing, _ = await _local_git(
        ["-C", str(main_repo), "worktree", "list", "--porcelain"]
    )
    registered = {
        Path(line[len("worktree ") :]).resolve()
        for line in listing.splitlines()
        if line.startswith("worktree ")
    }
    if code != 0 or target.resolve() not in registered:
        raise RuntimeError(f"任务工作区未注册到当前仓库的 worktree 列表：{target}")


async def _assert_reusable_local_workspace(root: Path, workspace: str) -> None:
    """Guard the reuse path: a bound workspace must still be safe to continue."""

    path = Path(workspace)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"任务工作区不存在或不可用：{workspace}；请显式重新执行以创建 Worktree")
    code, top, _ = await _local_git(["-C", str(path), "rev-parse", "--show-toplevel"])
    if code != 0:
        if await _git_common_dir(root) is not None:
            raise RuntimeError(f"任务工作区不是合法的 Git worktree：{workspace}")
        return  # A plain workspace is valid only when the project is non-git.
    if Path(top).resolve() != path.resolve():
        raise RuntimeError(f"任务工作区不是目录根：{workspace}")
    root_common = await _git_common_dir(root)
    if root_common is not None:
        await _assert_valid_task_worktree(root, path)


async def _ensure_local_task_worktree(root: Path, target: Path) -> None:
    if target.exists():
        # A residual worktree from a crashed run is safe to reuse once proven
        # valid; anything else is handled explicitly instead of silently reused.
        await _assert_valid_task_worktree(root, target)
        return
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root),
        "worktree",
        "add",
        "--detach",
        str(target),
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await process.communicate()
    if process.returncode:
        raise RuntimeError(error.decode(errors="replace"))


def _inherit_local_config(root: Path, target: Path, *, created: bool = False) -> None:
    """Copy project-owned config for a fresh worktree; preserve existing task config."""
    names = (Path("opencode.json"), Path("opencode.jsonc"),
             Path(".opencode/opencode.json"), Path(".opencode/opencode.jsonc"))
    if not created and any((target / name).exists() for name in names):
        return
    for name in names:
        source = root / name
        destination = target / name
        if source.is_file() and source != destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


async def prepare_workspace(task: Task, environment, *, reuse_existing: bool = True) -> str:
    """Prepare a task directory; environment relocation gets a distinct worktree."""
    settings = get_settings()
    local = environment.mode == ExecutionMode.LOCAL
    root = (
        str(settings.employee_workspace_path(environment.workspace_root).resolve())
        if local
        else environment.workspace_root
    )
    if reuse_existing and task.workspace:
        if local:
            await _assert_reusable_local_workspace(Path(root), task.workspace)
            _inherit_local_config(Path(root), Path(task.workspace))
        elif await _worker_channel(environment) == "worker":
            await _prepare_worker_workspace(
                environment, root=root, target=task.workspace, reuse_existing=True
            )
        else:
            raise RuntimeError("远程环境尚未安装 Worker")
        return task.workspace
    target_name = task.id if reuse_existing else f"{task.id}-env-{environment.id}"
    target = str(Path(root).parent / ".astra-tasks" / environment.employee_id / target_name)
    if local:
        if not Path(root).is_dir():
            raise FileNotFoundError(f"Workspace 不存在：{root}")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        created = not Path(target).exists()
        code, _, _ = await _local_git(["-C", root, "rev-parse", "--show-toplevel"])
        if code == 0:
            await _ensure_local_task_worktree(Path(root), Path(target))
        elif Path(target).exists():
            if Path(target).is_symlink() or not Path(target).is_dir():
                raise RuntimeError(f"任务工作区路径异常（非目录或符号链接）：{target}")
        else:
            Path(target).mkdir(exist_ok=False)
        _inherit_local_config(Path(root), Path(target), created=created)
    elif await _worker_channel(environment) == "worker":
        await _prepare_worker_workspace(environment, root=root, target=target, reuse_existing=False)
    else:
        raise RuntimeError("远程环境尚未安装 Worker")
    return target


async def _prepare_worker_workspace(environment, *, root: str, target: str, reuse_existing: bool) -> None:
    client = WorkerOpenCodeClient(get_settings(), environment_id=environment.id)
    await client.run_command(
        "prepare_workspace",
        {"root": root, "target": target, "reuse_existing": reuse_existing},
    )
