import pytest

from app.domain import TaskStatus
from app.state_machine import InvalidTransition, TaskStateMachine


def test_canceled_task_does_not_accept_late_success():
    with pytest.raises(InvalidTransition):
        TaskStateMachine.validate(TaskStatus.CANCELED, TaskStatus.SUCCEEDED)


def test_only_five_business_states_exist():
    assert {state.value for state in TaskStatus} == {
        "PENDING",
        "RUNNING",
        "WAITING",
        "SUCCEEDED",
        "CANCELED",
    }
