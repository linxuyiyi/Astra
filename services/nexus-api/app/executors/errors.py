from app.runtime.failure import FailureCode, failure_for


class OpenCodeExecutionError(RuntimeError):
    failure_code = FailureCode.SESSION_ERROR


class OpenCodeExecutionTimeout(OpenCodeExecutionError):
    failure_code = FailureCode.EXECUTOR_TIMEOUT


class OpenCodeServerUnavailable(OpenCodeExecutionError):
    failure_code = FailureCode.ENVIRONMENT_UNAVAILABLE


class OpenCodeSessionNotFound(OpenCodeExecutionError):
    failure_code = FailureCode.SESSION_NOT_FOUND

    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"OpenCode Session 不存在: {session_id}")


def classify_exception(exc: Exception):
    code = getattr(exc, "failure_code", None)
    if code is None:
        code = (
            FailureCode.EXECUTOR_TIMEOUT if isinstance(exc, TimeoutError) else FailureCode.EXECUTOR_EXCEPTION
        )
    return failure_for(code, str(exc))
