"""Read execution facts; review content is authored only by the assistant."""

from app.models import Task, TaskSummary


def summary_is_complete(summary: TaskSummary | None, task: Task) -> bool:
    """A summary only counts as the completed result when it is current.

    Currency is more than ``not stale``: the summary must belong to the Task's
    current execution round. An older Session's compensation must never be
    accepted as the result of a later round.
    """

    return bool(
        summary
        and not summary.stale
        and summary.source_status == task.status
        and summary.primary_session_id == task.current_session_id
        and summary.source_ref_snapshot.get("turn_anchor_id") == task.last_message_id
    )
