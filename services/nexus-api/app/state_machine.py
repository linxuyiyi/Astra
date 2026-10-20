from app.domain import TaskStatus


class InvalidTransition(ValueError):
    pass


class TaskStateMachine:
    """Business transitions. Runtime transport availability never changes task status."""

    transitions = {
        TaskStatus.PENDING: {
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        },
        TaskStatus.RUNNING: {
            TaskStatus.WAITING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        },
        TaskStatus.WAITING: {
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELED,
        },
        TaskStatus.SUCCEEDED: {TaskStatus.RUNNING, TaskStatus.WAITING, TaskStatus.FAILED, TaskStatus.PENDING},
        TaskStatus.FAILED: {
            TaskStatus.RUNNING,
            TaskStatus.WAITING,
            TaskStatus.SUCCEEDED,
            TaskStatus.PENDING,
            TaskStatus.CANCELED,
        },
        TaskStatus.CANCELED: {TaskStatus.RUNNING, TaskStatus.PENDING},
    }

    @classmethod
    def validate(cls, current, target):
        current, target = TaskStatus(current), TaskStatus(target)
        if current != target and target not in cls.transitions[current]:
            raise InvalidTransition(f"不允许从 {current} 流转到 {target}")
