from enum import StrEnum


class EmployeeStatus(StrEnum):
    OFFLINE = "OFFLINE"
    IDLE = "IDLE"
    WORKING = "WORKING"
    WAIT_HUMAN = "WAIT_HUMAN"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class RuntimeSessionStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"


class SessionTerminalReason(StrEnum):
    COMPLETED = "completed"
    ASSISTANT_ERROR = "assistant_error"
    NO_REPLY = "no_reply"
    INCOMPLETE = "incomplete"
    ABORTED = "aborted"
    TIMEOUT = "timeout"
    SESSION_MISSING = "session_missing"
    TRANSPORT_UNKNOWN = "transport_unknown"
    UNCONFIRMED = "unconfirmed"


class ExperienceOrigin(StrEnum):
    RUNTIME = "runtime"
    MANUAL = "manual"
    AUTO = "auto"


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
    UNAVAILABLE = "UNAVAILABLE"


class MaintenanceState(StrEnum):
    QUEUED = "QUEUED"
    PREFLIGHT = "PREFLIGHT"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    INTERRUPTED = "INTERRUPTED"


class MaintenanceAction(StrEnum):
    CHECK = "check"
    DEPLOY = "deploy"
    UPDATE = "update"
    RESTART = "restart"
    DISABLE = "disable"
    RESTORE = "restore"


class Priority(StrEnum):
    VERY_HIGH = "VERY_HIGH"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TaskType(StrEnum):
    ISSUE = "ISSUE"
    REQUIREMENT = "REQUIREMENT"


class AcceptanceStatus(StrEnum):
    SATISFIED = "SATISFIED"
    UNSATISFIED = "UNSATISFIED"
    UNCONFIRMED = "UNCONFIRMED"


class CompletionPolicy(StrEnum):
    AUTO = "AUTO"
    MANUAL_CONFIRM = "MANUAL_CONFIRM"


PRIORITY_WEIGHT = {
    Priority.VERY_HIGH: 4,
    Priority.HIGH: 3,
    Priority.MEDIUM: 2,
    Priority.LOW: 1,
}
