"""Add real message-time usage breakdowns and immutable Task completion fact.

Historical rows cannot recover per-message timestamps, so the backfill attributes
each existing Session's counters to its terminal/start day and single model. New
rows always compute the breakdown from native messages.
"""

from datetime import UTC, datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy import bindparam

revision = "0028_usage_breakdowns_and_task_completion"
down_revision = "0027_runtime_session_metrics"
branch_labels = None
depends_on = None


def _day(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value[:10] or None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).date().isoformat()
    return None


def _entry(label: str, key: str | None, row, total_key: str) -> dict | None:
    if key is None:
        return None
    return {
        label: key,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
        "total_tokens": row[total_key],
        "llm_call_count": row["llm_call_count"],
        "tool_call_count": row["tool_call_count"],
    }


def _has_usage(row) -> bool:
    return any(
        row[field] is not None
        for field in ("total_tokens", "llm_call_count", "tool_call_count")
    )


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("completed_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_tasks_completed_at", ["completed_at"])

    with op.batch_alter_table("runtime_sessions") as batch:
        batch.add_column(sa.Column("usage_by_day", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("model_usage", sa.JSON(), nullable=True))

    connection = op.get_bind()
    # Business completion existed only as the mutable Task.finished_at before.
    connection.execute(
        sa.text(
            "UPDATE tasks SET completed_at = finished_at "
            "WHERE status = 'SUCCEEDED' AND completed_at IS NULL AND finished_at IS NOT NULL"
        )
    )

    rows = (
        connection.execute(
            sa.text(
                "SELECT id, finished_at, started_at, created_at, model_name, "
                "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "total_tokens, llm_call_count, tool_call_count "
                "FROM runtime_sessions"
            )
        )
        .mappings()
        .all()
    )
    update = sa.text(
        "UPDATE runtime_sessions SET usage_by_day = :days, model_usage = :models "
        "WHERE id = :id"
    ).bindparams(bindparam("days", type_=sa.JSON), bindparam("models", type_=sa.JSON))
    for row in rows:
        if not _has_usage(row):
            continue
        day = _day(row["finished_at"] or row["started_at"] or row["created_at"])
        day_entry = _entry("date", day, row, "total_tokens") if day else None
        model_entry = _entry("model_name", row["model_name"] or "未知", row, "total_tokens")
        connection.execute(
            update,
            {
                "days": [day_entry] if day_entry else [],
                "models": [model_entry] if model_entry else [],
                "id": row["id"],
            },
        )


def downgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.drop_column("model_usage")
        batch.drop_column("usage_by_day")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_completed_at")
        batch.drop_column("completed_at")