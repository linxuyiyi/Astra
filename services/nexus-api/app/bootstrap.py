from datetime import UTC, datetime

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import EmployeeStatus, EnvironmentStatus
from app.models import AgentWorker, KnowledgeSpace, OpenCodeExecutionEnvironment, TaskCategory

DEFAULT_CATEGORIES = (
    ("testing-platform", "测试平台", "测试平台相关问题与需求", 10),
    ("automation-engineering", "自动化工程", "自动化工程建设与维护", 20),
    ("quality-defense", "防线运营", "质量防线运行与治理", 30),
)

DEFAULT_KNOWLEDGE_SPACES = (
    ("SPACE_PUBLIC", "公共知识库", "面向所有 Agent Worker 的通用知识", 10),
    ("SPACE_TESTING", "测试领域知识", "测试设计、执行与质量治理知识", 20),
    ("SPACE_DEVELOPMENT", "开发领域知识", "开发、集成与故障处理知识", 30),
    ("SPACE_PROJECT", "项目知识库", "项目约定、背景与交付知识", 40),
    ("SPACE_TOOLS", "工具与脚本", "可复用工具、脚本与使用指南", 50),
)

# 任务复盘产出的候选经验归入与任务分类对应的知识空间。
# 测试与质量治理进测试领域，工程实现进开发领域；未列出的分类进公共知识库。
CATEGORY_KNOWLEDGE_SPACE = {
    "testing-platform": "SPACE_TESTING",
    "automation-engineering": "SPACE_DEVELOPMENT",
    "quality-defense": "SPACE_TESTING",
}
DEFAULT_EXPERIENCE_SPACE = "SPACE_PUBLIC"


async def bootstrap_categories(session: AsyncSession) -> None:
    changed = False
    for category_id, name, description, sort_order in DEFAULT_CATEGORIES:
        if await session.get(TaskCategory, category_id):
            continue
        session.add(
            TaskCategory(
                id=category_id,
                name=name,
                description=description,
                enabled=True,
                sort_order=sort_order,
            )
        )
        changed = True
    if changed:
        await session.commit()


async def bootstrap_knowledge_spaces(session: AsyncSession) -> None:
    changed = False
    for space_id, name, description, sort_order in DEFAULT_KNOWLEDGE_SPACES:
        if await session.get(KnowledgeSpace, space_id):
            continue
        session.add(
            KnowledgeSpace(
                id=space_id,
                name=name,
                description=description,
                sort_order=sort_order,
                status="active",
                access_scope="public",
                created_by="system",
            )
        )
        changed = True
    if changed:
        await session.commit()


async def bootstrap_employees(session: AsyncSession, config_path) -> None:
    path = config_path / "employees.yaml"
    if not path.is_file():
        return
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    changed = False
    for item in payload.get("employees", []):
        if not item.get("enabled", True):
            continue
        employee = await session.get(AgentWorker, item["id"])
        if employee:
            continue
        now = datetime.now(UTC)
        employee = AgentWorker(
            id=item["id"],
            name=item.get("name", item["id"]),
            role=item.get("role", "Agent Worker"),
            description=item.get("description", ""),
            default_work_area=item.get("default_work_area", "development"),
            status=EmployeeStatus.IDLE,
            task_types=item.get("task_types", ["ISSUE", "REQUIREMENT"]),
            category_ids=item.get(
                "category_ids",
                ["testing-platform", "automation-engineering", "quality-defense"],
            ),
            enabled=item.get("enabled", True),
            auto_claim=item.get("policy", {}).get("auto_claim", True),
            auto_execute=item.get("policy", {}).get("auto_execute", True),
            online_since=now,
            last_heartbeat=now,
        )
        session.add(employee)
        await session.flush()
        environment = item.get("execution_environment", {})
        session.add(
            OpenCodeExecutionEnvironment(
                employee_id=employee.id,
                mode=environment.get("mode", "LOCAL"),
                name=environment.get("name", "本地 Agent 工作环境"),
                description=environment.get("description", ""),
                host=environment.get("host"),
                port=environment.get("port", 22),
                username=environment.get("username"),
                ssh_password=environment.get("ssh_password"),
                opencode_path=environment.get("opencode_path", "opencode"),
                model_name=environment.get("model_name"),
                workspace_root=environment.get("workspace_root", "./workspaces"),
                labels=environment.get("labels", []),
                status=EnvironmentStatus.UNKNOWN,
            )
        )
        changed = True
    if changed:
        await session.commit()
