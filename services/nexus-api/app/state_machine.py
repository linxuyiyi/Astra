from app.domain import TaskStatus


class InvalidTransition(ValueError):
    pass


class TaskStateMachine:
    transitions = {
        TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELED},
        TaskStatus.RUNNING: {TaskStatus.WAITING, TaskStatus.CANCELED, TaskStatus.FAILED},
        TaskStatus.WAITING: {
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.CANCELED,
            TaskStatus.FAILED,
        },
        TaskStatus.SUCCEEDED: set(),
        TaskStatus.CANCELED: {TaskStatus.RUNNING},
        TaskStatus.FAILED: {TaskStatus.RUNNING, TaskStatus.CANCELED},
    }

    @classmethod
    def validate(cls, previous, target):
        if previous != target and target not in cls.transitions[previous]:
            raise InvalidTransition(f"不能从 {previous} 变更为 {target}")
