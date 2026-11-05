import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentWorker, KnowledgeSpace, TaskCategory

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


async def bootstrap_workers(session: AsyncSession, config_path) -> None:
    from app.api.worker_routes import config_values
    from app.schemas import WorkerCreate

    path = config_path / "workers.yaml"
    if not path.is_file():
        return
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for item in payload.get("workers", []):
        data = WorkerCreate.model_validate(item)
        if await session.get(AgentWorker, data.id):
            continue
        session.add(AgentWorker(**config_values(data)))
    await session.commit()
