from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import Priority
from app.models import Task, TaskCategory
from app.schemas import TaskCreate
from app.services import TaskService
from app.sources.base import TaskSource


class TaskScout:
    def __init__(self, session: AsyncSession, sources: list[TaskSource]):
        self.session = session
        self.sources = sources

    async def poll_once(self) -> list[str]:
        imported: list[str] = []
        for source in self.sources:
            external_tasks = await source.discover_tasks()
            for item in external_tasks:
                category = await self.session.get(TaskCategory, item.category_id)
                if not category or not category.enabled:
                    continue
                exists = await self.session.scalar(
                    select(Task.id).where(
                        Task.source == source.id,
                        Task.external_task_id == item.external_task_id,
                    )
                )
                if exists:
                    continue
                try:
                    task = await TaskService(self.session).create(
                        TaskCreate(
                            source=source.id,
                            external_task_id=item.external_task_id,
                            task_type=item.task_type,
                            category_id=item.category_id,
                            title=item.title,
                            description=item.description or item.title,
                            acceptance_criteria=[
                                {"content": content} for content in item.acceptance_criteria
                            ],
                            attachments=item.attachments,
                            priority=Priority(item.priority),
                        )
                    )
                except (IntegrityError, ValueError):
                    continue
                imported.append(task.id)
        return imported
