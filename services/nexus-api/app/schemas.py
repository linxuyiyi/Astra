from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import ExecutionMode, Priority, TaskType


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class WorkerWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    enabled: bool = True
    task_types: list[TaskType] = Field(default_factory=lambda: list(TaskType))
    category_ids: list[str] = Field(default_factory=list)
    runtime_engine: Literal["opencode"] = "opencode"
    mode: ExecutionMode = ExecutionMode.LINUX_REMOTE
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None
    ssh_password: str | None = Field(default=None, max_length=1024)
    model_name: str | None = None
    workspace_root: str = "/opt/nexus/workspace"
    repository_url: str | None = None
    git_token: str | None = Field(default=None, max_length=4096)
    branch: str | None = None
    skill_refs: list[str] = Field(default_factory=list)
    mcp_refs: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def remote_target(self):
        if self.mode == ExecutionMode.LINUX_REMOTE:
            if not self.host or not self.username or not self.workspace_root.startswith("/"):
                raise ValueError("远程 Worker 需要 Host、Username 和绝对 Workspace 路径")
        return self


class WorkerCreate(WorkerWrite):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class WorkerOccupiedTask(ORMModel):
    id: str
    title: str


class WorkerRead(ORMModel):
    id: str
    name: str
    description: str
    enabled: bool
    status: str
    task_types: list[str]
    category_ids: list[str]
    runtime_engine: str
    mode: str
    host: str | None
    port: int
    username: str | None
    has_ssh_password: bool
    model_name: str | None
    workspace_root: str
    repository_url: str | None
    has_git_token: bool
    branch: str | None
    skill_refs: list[str]
    mcp_refs: list[dict[str, Any]]
    epoch: int
    last_heartbeat_at: datetime | None
    agent_version: str | None
    online: bool | None = None
    occupied_tasks: list[WorkerOccupiedTask] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TaskCategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    task_prompt: str | None = None
    question_prompt: str | None = None
    enabled: bool = True
    sort_order: int = 0


class TaskCategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    task_prompt: str | None = None
    question_prompt: str | None = None
    enabled: bool | None = None
    sort_order: int | None = None


class TaskCategoryRead(ORMModel):
    id: str
    name: str
    description: str
    task_prompt: str | None
    question_prompt: str | None
    enabled: bool
    sort_order: int
    created_at: datetime
    updated_at: datetime


class AcceptanceCriterion(BaseModel):
    id: str | None = None
    content: str = Field(min_length=1, max_length=500)


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["manual", "opencode", "private_chat", "group_chat", "nr_test_system"] = "manual"
    external_task_id: str | None = None
    origin_employee_id: str | None = None
    external_reference_id: str | None = None
    task_type: TaskType
    category_id: str
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    agent_worker_id: str | None = None
    priority: Priority = Priority.MEDIUM


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, min_length=1)
    priority: Priority | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    agent_worker_id: str | None = None
    origin_employee_id: str | None = None


class TaskRead(ORMModel):
    id: str
    source: str
    external_task_id: str | None
    origin_employee_id: str | None
    external_reference_id: str | None
    task_type: str
    category_id: str | None
    title: str
    description: str
    acceptance_criteria: list[AcceptanceCriterion]
    attachments: list[dict[str, Any]]
    priority: str
    status: str
    agent_worker_id: str | None
    worker_name: str | None = None
    worker_online: bool | None = None
    worker_last_heartbeat_at: datetime | None = None
    current_session_id: str | None
    workspace: str | None
    last_message_id: str | None
    pending_message_id: str | None
    waiting_reason: str | None
    current_progress: str
    execution_released: bool
    last_activity_at: datetime | None
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExecutionStopConfirm(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class EventRead(ORMModel):
    id: int
    task_id: str | None
    employee_id: str | None
    event_type: str
    phase: str
    message: str
    detail: dict[str, Any]
    created_at: datetime
