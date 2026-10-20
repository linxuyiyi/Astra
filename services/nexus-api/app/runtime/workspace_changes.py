"""Collect deterministic Workspace/Git facts."""

from __future__ import annotations

import asyncio
import difflib
from pathlib import Path

from sqlalchemy import select

from app.domain import ExecutionMode
from app.models import RuntimeSession
from app.runtime.session_connection import resolve_environment
from app.runtime.workspace_files import MAX_UPLOAD, validate_path


async def _git(environment, workspace: str, *args: str) -> tuple[int, str]:
    """Return Git stdout without stripping porcelain-leading status bytes."""

    from app.runtime.worker_files import environment_uses_worker, worker_git

    if await environment_uses_worker(environment):
        return await worker_git(environment, workspace, args)
    command = ["git", "-C", workspace, *args]
    if environment.mode == ExecutionMode.LOCAL:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await process.communicate()
        return process.returncode, output.decode("utf-8", errors="replace")
    raise RuntimeError("远程环境尚未安装 Worker")


async def workspace_head_commit(environment, workspace: str) -> str | None:
    try:
        code, output = await _git(environment, workspace, "rev-parse", "HEAD")
        value = output.strip()
        return value if code == 0 and value else None
    except Exception:
        return None


async def _task_baseline(session, task_id: str, workspace: str | None = None) -> str | None:
    return await session.scalar(
        select(RuntimeSession.workspace_base_commit)
        .where(
            RuntimeSession.task_id == task_id,
            RuntimeSession.workspace_base_commit.is_not(None),
            *([RuntimeSession.workspace == workspace] if workspace else []),
        )
        .order_by(RuntimeSession.created_at, RuntimeSession.id)
        .limit(1)
    )


async def collect_workspace_changes(session, task) -> dict:
    """Return task-wide Git facts relative to the first Session baseline."""

    if not task.workspace or not task.execution_environment_snapshot:
        return {}
    try:
        environment = await resolve_environment(session, task)
        code, inside = await _git(environment, task.workspace, "rev-parse", "--is-inside-work-tree")
        if code or inside.strip().lower() != "true":
            return {}

        files: set[str] = set()
        added: set[str] = set()
        deleted: set[str] = set()

        code, porcelain = await _git(
            environment, task.workspace, "status", "--porcelain=v1", "-z",
            "--no-renames", "--untracked-files=all",
        )
        if code == 0:
            for entry in porcelain.split("\0"):
                if len(entry) < 4:
                    continue
                status, path = entry[:2], entry[3:]
                files.add(path)
                if "?" in status or "A" in status:
                    added.add(path)
                if "D" in status:
                    deleted.add(path)

        baseline = await _task_baseline(session, task.id, task.workspace)
        commits: list[str] = []
        if baseline:
            code, raw_commits = await _git(
                environment,
                task.workspace,
                "rev-list",
                "--reverse",
                "--max-count=200",
                f"{baseline}..HEAD",
            )
            if code == 0:
                commits = [line for line in raw_commits.splitlines() if line]
            diff_code, changed = await _git(
                environment,
                task.workspace,
                "diff",
                "--name-status",
                "--no-renames",
                "-z",
                f"{baseline}..HEAD",
            )
            if diff_code == 0:
                entries = changed.split("\0")
                for status, path in zip(entries[::2], entries[1::2], strict=False):
                    if not path:
                        continue
                    files.add(path)
                    if status.startswith("A"):
                        added.add(path)
                    if status.startswith("D"):
                        deleted.add(path)

        if not files and not commits:
            return {}
        return {
            "baseline_commit": baseline,
            "changed_files": len(files),
            "added_files": len(added),
            "deleted_files": len(deleted),
            "files": sorted(files),
            "commits": commits,
        }
    except Exception:
        # Summary generation must never change a valid Session outcome merely
        # because the Workspace is no longer reachable.
        return {}


async def _working_text(environment, workspace: str, file: str) -> str:
    from app.runtime.worker_files import environment_uses_worker, worker_read_text

    if await environment_uses_worker(environment):
        await validate_path(environment, workspace, file)
        return await worker_read_text(environment, workspace, file)
    relative = await validate_path(environment, workspace, file)
    if environment.mode == ExecutionMode.LOCAL:
        target = Path(workspace) / relative
        if not target.is_file():
            return ""
        if target.stat().st_size > MAX_UPLOAD:
            raise ValueError("Diff 文件不能超过 20 MB")
        data = await asyncio.to_thread(target.read_bytes)
    else:
        raise RuntimeError("远程环境尚未安装 Worker")
    if len(data) > MAX_UPLOAD:
        raise ValueError("Diff 文件不能超过 20 MB")
    return data.decode("utf-8", errors="replace")


async def workspace_diff(
    session, task, *, cumulative: bool = True, directory: str | None = None,
    environment=None, workspace: str | None = None,
) -> list[dict]:
    """Native diff shape, read directly from the bound Git worktree on either host."""
    environment = environment or await resolve_environment(session, task)
    workspace = workspace or task.workspace
    baseline = await _task_baseline(session, task.id, workspace) if cumulative else None
    await session.commit()
    code, _ = await _git(environment, workspace, "rev-parse", "--is-inside-work-tree")
    if code:
        return []
    revision = baseline or "HEAD"
    path = await validate_path(environment, workspace, directory or ".")
    code, stats = await _git(
        environment, workspace, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
        "--numstat", "-z", revision, "--", path,
    )
    if code:
        raise ValueError("任务 Git 基线不可读取，无法展示累计变更")
    files: dict[str, tuple[int, int]] = {}
    for entry in stats.split("\0"):
        fields = entry.split("\t", 2)
        if len(fields) == 3:
            additions, deletions, file = fields
            files[file] = (int(additions) if additions.isdigit() else 0,
                           int(deletions) if deletions.isdigit() else 0)
    code, untracked = await _git(environment, workspace, "ls-files", "--others", "--exclude-standard", "-z",
                                 "--", path)
    if code == 0:
        for file in untracked.split("\0"):
            if file:
                files.setdefault(file, (0, 0))
    result = []
    for file, (additions, deletions) in sorted(files.items()):
        # Never read a worktree symlink into another project.
        await validate_path(environment, workspace, file)
        before_code, before = await _git(environment, workspace, "show", f"{revision}:{file}")
        after = await _working_text(environment, workspace, file)
        patch_code, patch = await _git(
            environment, workspace, "diff", "--no-ext-diff", "--no-textconv", "--no-color",
            "--no-renames", revision, "--", file,
        )
        if patch_code:
            raise ValueError("Git Diff 读取失败")
        if before_code and after:
            before = ""
            additions = len(after.splitlines())
            patch = "".join(difflib.unified_diff([], after.splitlines(keepends=True),
                                               fromfile="/dev/null", tofile=f"b/{file}"))
        _, name_status = await _git(environment, workspace, "diff", "--no-renames", "--name-status",
                                    "-z", revision, "--", file)
        status = "added" if before_code else "deleted" if name_status.startswith("D\0") else "modified"
        result.append({"file": file, "patch": patch, "before": before, "after": after,
                       "additions": additions, "deletions": deletions, "status": status})
    return result
