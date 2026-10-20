"""Shared failure vocabulary and recovery policy for every runtime boundary."""

from dataclasses import asdict, dataclass
from enum import StrEnum


class FailureCode(StrEnum):
    DISPATCH_FAILED = "DISPATCH_FAILED"
    WORKER_STARTUP_LOST = "WORKER_STARTUP_LOST"
    WORKER_LEASE_LOST = "WORKER_LEASE_LOST"
    EXECUTOR_TIMEOUT = "EXECUTOR_TIMEOUT"
    EXECUTOR_EXCEPTION = "EXECUTOR_EXCEPTION"
    NONZERO_EXIT = "NONZERO_EXIT"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_ERROR = "SESSION_ERROR"
    ENVIRONMENT_UNAVAILABLE = "ENVIRONMENT_UNAVAILABLE"
    USER_CANCELLED = "USER_CANCELLED"
    USER_SUSPENDED = "USER_SUSPENDED"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"


class FailureScope(StrEnum):
    DISPATCH = "DISPATCH"
    WORKER = "WORKER"
    EXECUTOR = "EXECUTOR"
    SESSION = "SESSION"
    ENVIRONMENT = "ENVIRONMENT"
    USER = "USER"
    SYSTEM = "SYSTEM"


class RecoveryAction(StrEnum):
    NONE = "NONE"
    RETRY = "RETRY"
    RESUME_SESSION = "RESUME_SESSION"
    CREATE_SESSION = "CREATE_SESSION"
    WAIT_HUMAN = "WAIT_HUMAN"
    WAIT_ENVIRONMENT = "WAIT_ENVIRONMENT"
    FAIL = "FAIL"


POLICY = {
    FailureCode.DISPATCH_FAILED: (FailureScope.DISPATCH, True, RecoveryAction.RETRY),
    FailureCode.WORKER_STARTUP_LOST: (FailureScope.WORKER, True, RecoveryAction.RETRY),
    FailureCode.WORKER_LEASE_LOST: (FailureScope.WORKER, True, RecoveryAction.CREATE_SESSION),
    FailureCode.EXECUTOR_TIMEOUT: (FailureScope.EXECUTOR, True, RecoveryAction.RESUME_SESSION),
    FailureCode.EXECUTOR_EXCEPTION: (FailureScope.EXECUTOR, True, RecoveryAction.RESUME_SESSION),
    FailureCode.NONZERO_EXIT: (FailureScope.EXECUTOR, True, RecoveryAction.RETRY),
    FailureCode.SESSION_NOT_FOUND: (FailureScope.SESSION, True, RecoveryAction.CREATE_SESSION),
    FailureCode.SESSION_ERROR: (FailureScope.SESSION, True, RecoveryAction.RESUME_SESSION),
    FailureCode.ENVIRONMENT_UNAVAILABLE: (FailureScope.ENVIRONMENT, True, RecoveryAction.WAIT_ENVIRONMENT),
    FailureCode.USER_CANCELLED: (FailureScope.USER, False, RecoveryAction.NONE),
    FailureCode.USER_SUSPENDED: (FailureScope.USER, False, RecoveryAction.NONE),
    FailureCode.INVARIANT_VIOLATION: (FailureScope.SYSTEM, False, RecoveryAction.FAIL),
}


@dataclass(frozen=True)
class RuntimeFailure:
    failure_code: FailureCode
    failure_scope: FailureScope
    retryable: bool
    recovery_action: RecoveryAction
    raw_error: str

    def as_dict(self) -> dict:
        return asdict(self)


def failure_for(code: FailureCode | str, raw_error: str = "") -> RuntimeFailure:
    code = FailureCode(code)
    return RuntimeFailure(code, *POLICY[code], raw_error[:4000])


def record_failure(run, code: FailureCode | str | None, raw_error: str = "") -> None:
    failure = failure_for(code, raw_error) if code else None
    for field in ("failure_code", "failure_scope", "retryable", "recovery_action", "raw_error"):
        setattr(run, field, getattr(failure, field) if failure else None)
