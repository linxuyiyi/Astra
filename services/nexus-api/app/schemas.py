from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain import (
    CompletionPolicy,
    ExecutionMode,
    Priority,
    TaskType,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RuntimeRead(ORMModel):
    runtime_state: str
    runtime_status_label: str
    runtime_detail: str
    failure: dict[str, Any] | None


class ExecutionEnvironmentWrite(BaseModel):
    mode: ExecutionMode = ExecutionMode.LOCAL
    name: str = "本地 Agent 工作环境"
    description: str = ""
    host: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = None
    ssh_password: str | None = Field(default=None, max_length=1024)
    opencode_server_port: int = Field(default=14096, ge=1, le=65535)
    opencode_path: str = Field(default="opencode", min_length=1)
    model_name: str | None = Field(default=None, max_length=255)
    workspace_root: str = Field(default="./workspaces", min_length=1)
    repository_id: str | None = Field(default=None, max_length=64)
    branch: str | None = Field(default=None, max_length=255)
    labels: list[str] = Field(default_factory=list)
    skill_refs: list[str] | None = None
    mcp_refs: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def validate_remote(self):
        if self.mode is ExecutionMode.LINUX_REMOTE:
            missing = [
                label
                for label, value in {
                    "环境名称": self.name,
                    "Host": self.host,
                    "Username": self.username,
                }.items()
                if not value
            ]
            if missing:
                raise ValueError(f"Linux 远程环境缺少：{', '.join(missing)}")
            if not self.workspace_root.startswith("/"):
                raise ValueError("Linux 远程 Workspace 根目录必须使用绝对路径")
        return self


class ExecutionEnvironmentRead(ORMModel):
    id: int
    employee_id: str
    mode: str
    name: str
    description: str
    host: str | None
    port: int
    username: str | None
    has_ssh_password: bool
    opencode_server_port: int
    opencode_path: str
    model_name: str | None
    workspace_root: str
    repository_id: str | None = None
    repository_url: str | None = None
    repository_branch: str | None = None
    branch: str | None = None
    skill_refs: list[str] = Field(default_factory=list)
    mcp_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    status_detail: dict[str, Any]
    labels: list[str]
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UsageSummary(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    total_tokens: int | None = None
    llm_call_count: int | None = None
    tool_call_count: int | None = None
    tool_success_count: int = 0
    tool_failure_count: int = 0
    duration_seconds: int = 0
    execution_seconds: int | None = None
    idle_seconds: int | None = None
    waiting_seconds: int = 0
    completed_tasks: int = 0
    task_count: int = 0
    session_count: int = 0
    native_session_count: int = 0
    completed_sessions: int = 0
    failed_sessions: int = 0
    interrupted_sessions: int = 0


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


class EmployeeCreate(BaseModel):
    id: str
    digital_employee_id: str | None = Field(default=None, max_length=64)
    name: str
    role: str
    description: str = ""
    character_id: int = Field(default=0, ge=0, le=8, strict=True)
    default_work_area: Literal[
        "development", "collaboration", "pantry", "integration", "meeting", "special"
    ] = "development"
    task_types: list[TaskType] = Field(default_factory=lambda: list(TaskType), min_length=1)
    category_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    auto_claim: bool = True
    auto_execute: bool = True
    execution_environment: ExecutionEnvironmentWrite = Field(default_factory=ExecutionEnvironmentWrite)


class EmployeePatch(BaseModel):
    digital_employee_id: str | None = Field(default=None, max_length=64)
    name: str | None = None
    role: str | None = None
    description: str | None = None
    character_id: int = Field(default=0, ge=0, le=8, strict=True)
    default_work_area: (
        Literal["development", "collaboration", "pantry", "integration", "meeting", "special"] | None
    ) = None
    task_types: list[TaskType] | None = Field(default=None, min_length=1)
    category_ids: list[str] | None = None
    enabled: bool | None = None
    auto_claim: bool | None = None
    auto_execute: bool | None = None

    @field_validator("task_types")
    @classmethod
    def require_task_types(cls, value):
        if value is None:
            raise ValueError("Worker 至少需要一种支持的任务类型")
        return value


class ChannelHealth(BaseModel):
    worker_online: bool
    opencode_reachable: bool
    repository_bound: bool
    executing: bool
    waiting_human: bool
    availability: str


class EmployeeRead(RuntimeRead):
    id: str
    digital_employee_id: str | None = None
    name: str
    role: str
    description: str
    character_id: int
    default_work_area: str
    status: str
    task_types: list[str]
    category_ids: list[str]
    enabled: bool
    auto_claim: bool
    auto_execute: bool
    current_task_count: int
    last_heartbeat: datetime | None
    online_since: datetime | None
    created_at: datetime
    updated_at: datetime
    execution_environment: ExecutionEnvironmentRead | None = None
    channel_health: ChannelHealth = Field(
        default_factory=lambda: ChannelHealth(
            worker_online=True,
            opencode_reachable=False,
            repository_bound=False,
            executing=False,
            waiting_human=False,
            availability="UNKNOWN",
        )
    )
    today_usage: UsageSummary = Field(default_factory=UsageSummary)
    today_completed: int = 0


class AcceptanceCriterion(BaseModel):
    id: str | None = None
    content: str = Field(min_length=1, max_length=500)






class RelatedTaskPart(BaseModel):
    repository_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    depends_on: int | None = Field(default=None, ge=0)


class RelatedTaskCreate(BaseModel):
    task_type: TaskType
    category_id: str
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    parts: list[RelatedTaskPart] = Field(min_length=1)


class TaskCreate(BaseModel):
    source: str = "manual"
    external_task_id: str | None = None
    task_type: TaskType
    category_id: str
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(min_length=1)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    employee_id: str | None = None
    responsible_employee_id: str | None = None
    source_session_id: str | None = None
    source_conversation_id: str | None = None
    source_channel_id: str | None = None
    idempotency_key: str | None = None
    repository_id: str | None = None
    priority: Priority = Priority.MEDIUM
    # Kept in the API for stored tasks; all new tasks close explicitly.
    completion_policy: CompletionPolicy = CompletionPolicy.MANUAL_CONFIRM


class ExecutionStopConfirm(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class RetrySubmission(BaseModel):
    confirm_unknown: bool = False


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, min_length=1)
    priority: Priority | None = None
    acceptance_criteria: list[AcceptanceCriterion] | None = None
    employee_id: str | None = None
    responsible_employee_id: str | None = None


class TaskRead(RuntimeRead):
    id: str
    source: str
    external_task_id: str | None
    task_type: str
    category_id: str | None
    title: str
    description: str
    acceptance_criteria: list[AcceptanceCriterion]
    attachments: list[dict[str, Any]]
    priority: str
    completion_policy: str
    status: str
    employee_id: str | None
    worker_id: str | None = None
    responsible_employee_id: str | None = None
    source_session_id: str | None = None
    source_conversation_id: str | None = None
    source_channel_id: str | None = None
    idempotency_key: str | None = None
    repository_id: str | None = None
    related_task_id: str | None = None
    base_commit: str | None = None
    current_session_id: str | None
    session_status: str | None = None
    session_terminal_reason: str | None = None
    workspace: str | None
    runtime_connection: str
    pending_message_id: str | None
    runtime_error: str | None
    current_progress: str
    suspension: dict[str, Any] | None
    execution_error: dict[str, Any] | None
    inactivity_alert: bool
    inactivity_alert_since: datetime | None
    last_activity_at: datetime | None
    submitted_at: datetime | None
    confirmed_at: datetime | None
    failure_reason: str | None
    human_required_reason: str | None
    waiting_reason: str | None = None
    human_intervention: dict[str, Any] | None
    execution_environment_snapshot: dict[str, Any] | None
    claimed_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


















class EventRead(ORMModel):
    id: int
    task_id: str | None
    employee_id: str | None
    event_type: str
    phase: str
    message: str
    detail: dict[str, Any]
    created_at: datetime

















