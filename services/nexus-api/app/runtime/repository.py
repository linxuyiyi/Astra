"""Worker Git 凭据、工作区事实与 Worker 复制。

Git 凭据仅提供加解密入口；实际 Git 操作由 Worker 的部署和运行逻辑处理。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain import EmployeeStatus, EnvironmentStatus
from app.gateway.security import CredentialCipher
from app.models import AgentWorker, OpenCodeExecutionEnvironment, Task


class WorkerCopyError(RuntimeError):
    pass


def cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().gateway_master_key)


def read_base_commit(workspace: str) -> str:
    """读取已有仓库的 HEAD。不是仓库时返回空字符串，不执行 git init。"""

    if not workspace:
        return ""
    completed = subprocess.run(
        ["git", "-C", workspace, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def fresh_workspace(source: str, employee_id: str) -> str:
    if source.startswith("/"):
        parent = source.rsplit("/", 1)[0] or "/"
        return f"{parent}/{employee_id}"
    return f"./workspaces/{employee_id}"


def collect_skill_files(names: list[str], source: Path) -> list[dict[str, str]]:
    """从平台技能目录复制引用。不读取仓库里的 `.opencode/`。"""

    files: list[dict[str, str]] = []
    for name in names:
        path = source / name / "SKILL.md"
        if path.is_file():
            files.append({"name": name, "body": path.read_text(encoding="utf-8")})
    return files


def dependency_order(tasks: list[Task]) -> list[Task]:
    ids = {task.id for task in tasks}
    children: dict[str, list[Task]] = {task.id: [] for task in tasks}
    indegree = {task.id: 0 for task in tasks}
    for task in tasks:
        parent = task.related_task_id
        if parent and parent in ids:
            children[parent].append(task)
            indegree[task.id] += 1
    ready = [task for task in tasks if indegree[task.id] == 0]
    ordered: list[Task] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in children[current.id]:
            indegree[child.id] -= 1
            if indegree[child.id] == 0:
                ready.append(child)
    return ordered


async def related_tasks(session: AsyncSession, task_id: str) -> list[Task]:
    start = await session.get(Task, task_id)
    if start is None:
        return []
    group = {start.id: start}
    current = start
    while current.related_task_id and current.related_task_id not in group:
        parent = await session.get(Task, current.related_task_id)
        if parent is None:
            break
        group[parent.id] = parent
        current = parent
    changed = True
    while changed:
        changed = False
        rows = (await session.scalars(select(Task).where(Task.related_task_id.in_(list(group))))).all()
        for row in rows:
            if row.id not in group:
                group[row.id] = row
                changed = True
    return dependency_order(list(group.values()))


async def free_opencode_port(session: AsyncSession, host: str | None, preferred: int = 14096) -> int:
    query = select(OpenCodeExecutionEnvironment.opencode_server_port)
    if host is None:
        query = query.where(OpenCodeExecutionEnvironment.host.is_(None))
    else:
        query = query.where(OpenCodeExecutionEnvironment.host == host)
    used = set(await session.scalars(query))
    if preferred not in used:
        return preferred
    port = preferred + 1
    while port in used and port <= 65535:
        port += 1
    if port > 65535 or port in used:
        raise WorkerCopyError("没有可用的 OpenCode 端口")
    return port


async def copy_worker(
    session: AsyncSession,
    source: AgentWorker,
    *,
    new_id: str,
    name: str,
    port: int | None = None,
) -> AgentWorker:
    environment = source.execution_environment
    if environment is None:
        raise WorkerCopyError("员工尚未配置 Agent 工作环境")
    if await session.get(AgentWorker, new_id):
        raise WorkerCopyError("员工 ID 已存在")
    employee = AgentWorker(
        id=new_id,
        name=name,
        role=source.role,
        description=source.description,
        character_id=source.character_id,
        default_work_area=source.default_work_area,
        status=EmployeeStatus.IDLE,
        task_types=list(source.task_types or []),
        category_ids=list(source.category_ids or []),
        enabled=source.enabled,
        auto_claim=source.auto_claim,
        auto_execute=source.auto_execute,
        current_task_count=0,
    )
    session.add(employee)
    await session.flush()
    chosen_port = port if port is not None else await free_opencode_port(session, environment.host)
    session.add(
        OpenCodeExecutionEnvironment(
            employee_id=employee.id,
            mode=environment.mode,
            name=environment.name,
            description=environment.description,
            host=environment.host,
            port=22,
            username=environment.username,
            ssh_password=None,
            opencode_server_port=chosen_port,
            opencode_path=environment.opencode_path,
            model_name=environment.model_name,
            workspace_root=fresh_workspace(environment.workspace_root, employee.id),
            branch=environment.branch,
            repository_url=environment.repository_url,
            git_token_ciphertext=None,
            skill_refs=list(environment.skill_refs or []),
            mcp_refs=[dict(item) for item in environment.mcp_refs or [] if isinstance(item, dict)],
            labels=list(environment.labels or []),
            status=EnvironmentStatus.UNKNOWN,
            status_detail={},
        )
    )
    await session.commit()
    return employee
