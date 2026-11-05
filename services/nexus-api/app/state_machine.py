from app.domain import TaskStatus


class InvalidTransition(ValueError):
    pass


class TaskStateMachine:
    transitions = {
        TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELED},
        TaskStatus.RUNNING: {TaskStatus.WAITING, TaskStatus.CANCELED},
        TaskStatus.WAITING: {TaskStatus.RUNNING, TaskStatus.SUCCEEDED, TaskStatus.CANCELED},
        TaskStatus.SUCCEEDED: {TaskStatus.RUNNING},
        TaskStatus.CANCELED: {TaskStatus.RUNNING},
    }

    @classmethod
    def validate(cls, previous, target):
        if previous != target and target not in cls.transitions[previous]:
            raise InvalidTransition(f"不能从 {previous} 变更为 {target}")
