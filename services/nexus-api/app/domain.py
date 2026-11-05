from enum import StrEnum


class WorkerStatus(StrEnum):
    OFFLINE = "OFFLINE"
    IDLE = "IDLE"
    WORKING = "WORKING"
    PAUSED = "PAUSED"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    CANCELED = "CANCELED"


class ExperienceType(StrEnum):
    ROOT_CAUSE = "root_cause"
    FIX = "fix"
    ENVIRONMENT = "environment"
    TEST_RULE = "test_rule"
    TOOL_USAGE = "tool_usage"
    OTHER = "other"


class ExecutionMode(StrEnum):
    LOCAL = "LOCAL"
    LINUX_REMOTE = "LINUX_REMOTE"


class EnvironmentStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    CHECKING = "CHECKING"
    AVAILABLE = "AVAILABLE"
    DRIFTED = "DRIFTED"
    UNAVAILABLE = "UNAVAILABLE"


class WorkerReadiness(StrEnum):
    READY = "READY"
    DRIFTED = "DRIFTED"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class MaintenanceState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class MaintenanceAction(StrEnum):
    CHECK = "check"
    DEPLOY = "deploy"
    UPDATE = "update"
    RESTART = "restart"


class Priority(StrEnum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TaskType(StrEnum):
    ISSUE = "ISSUE"
    REQUIREMENT = "REQUIREMENT"
