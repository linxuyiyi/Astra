from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ExternalTask:
    external_task_id: str
    title: str
    task_type: str = "REQUIREMENT"
    category_id: str = "testing-platform"
    description: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    priority: str = "MEDIUM"


class TaskSource(ABC):
    id: str

    @abstractmethod
    async def discover_tasks(self) -> list[ExternalTask]: ...

    @abstractmethod
    async def get_task(self, external_task_id: str) -> ExternalTask: ...

    @abstractmethod
    async def update_task(self, external_task_id: str, result: dict[str, Any]) -> None: ...

    @abstractmethod
    async def heartbeat(self) -> bool: ...
