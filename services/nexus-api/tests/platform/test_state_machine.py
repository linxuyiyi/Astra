import pytest

from app.domain import TaskStatus
from app.state_machine import InvalidTransition, TaskStateMachine


def test_canceled_task_does_not_accept_late_success():
    with pytest.raises(InvalidTransition):
        TaskStateMachine.validate(TaskStatus.CANCELED, TaskStatus.SUCCEEDED)


def test_succeeded_task_cannot_transition_back_to_running():
    with pytest.raises(InvalidTransition):
        TaskStateMachine.validate(TaskStatus.SUCCEEDED, TaskStatus.RUNNING)


def test_only_six_business_states_exist():
    assert {state.value for state in TaskStatus} == {
        "PENDING",
        "RUNNING",
        "WAITING",
        "SUCCEEDED",
        "CANCELED",
        "FAILED",
    }
