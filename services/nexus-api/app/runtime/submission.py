"""First-send and human-retry evidence for one pending prompt.

The only proof that a prompt was not sent is a stored attempt whose request
body was never fetched. A fetched body is an unknown result.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import Task


def attempt_key(message_id: str, attempt: int) -> str:
    return f"{message_id}:{attempt}"


def attempts_map(task: Task) -> dict:
    raw = task.submission_attempts
    return dict(raw) if isinstance(raw, dict) else {}


def get_attempt(task: Task, message_id: str, attempt: int) -> dict | None:
    item = attempts_map(task).get(attempt_key(message_id, attempt))
    return dict(item) if isinstance(item, dict) else None


def put_attempt(task: Task, message_id: str, attempt: int, request_id: str) -> dict:
    data = attempts_map(task)
    data[attempt_key(message_id, attempt)] = {
        "request_id": request_id,
        "body_fetched_at": None,
    }
    task.submission_attempts = data
    return data


def mark_body_fetched(task: Task, message_id: str, attempt: int, when: datetime) -> None:
    data = attempts_map(task)
    key = attempt_key(message_id, attempt)
    item = dict(data.get(key) or {})
    if not item.get("request_id"):
        raise KeyError(key)
    stamp = when if when.tzinfo else when.replace(tzinfo=UTC)
    item["body_fetched_at"] = stamp.astimezone(UTC).isoformat()
    data[key] = item
    task.submission_attempts = data


def body_fetched_at(task: Task, message_id: str, attempt: int) -> datetime | None:
    item = get_attempt(task, message_id, attempt)
    if not item:
        return None
    raw = item.get("body_fetched_at")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
