import pytest

from app.domain import TaskStatus
from app.state_machine import InvalidTransition, TaskStateMachine


@pytest.mark.parametrize(
    "current", [TaskStatus.FAILED, TaskStatus.WAITING, TaskStatus.SUCCEEDED, TaskStatus.CANCELED]
)
def test_explicit_continue_is_supported(current):
    TaskStateMachine.validate(current, TaskStatus.RUNNING)


@pytest.mark.parametrize("target", [TaskStatus.WAITING, TaskStatus.SUCCEEDED, TaskStatus.FAILED])
def test_native_session_outcomes_are_business_states(target):
    TaskStateMachine.validate(TaskStatus.RUNNING, target)


def test_canceled_task_does_not_accept_late_success():
    with pytest.raises(InvalidTransition):
        TaskStateMachine.validate(TaskStatus.CANCELED, TaskStatus.SUCCEEDED)


def test_only_six_business_states_exist():
    assert {state.value for state in TaskStatus} == {
        "PENDING",
        "RUNNING",
        "WAITING",
        "SUCCEEDED",
        "FAILED",
        "CANCELED",
    }
