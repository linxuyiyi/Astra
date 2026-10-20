"""RuntimeSession aggregate usage for Dashboard/Usage on the Session fact source."""

from alembic import op
import sqlalchemy as sa

revision = "0023_runtime_session_usage"
down_revision = "0022_task_summary_experience"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("llm_call_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tool_usage", sa.JSON(), nullable=True))


    db = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=db)
    tools = sa.Table("tool_usages", metadata, autoload_with=db)
    sessions = sa.Table("runtime_sessions", metadata, autoload_with=db)

    rows = (
        db.execute(
            sa.select(runs)
            .where(runs.c.opencode_session_id.is_not(None))
            .order_by(runs.c.task_id, runs.c.run_number)
        )
        .mappings()
        .all()
    )
    latest_by_session = {}
    for row in rows:
        latest_by_session[row["opencode_session_id"]] = row

    for session_id, row in latest_by_session.items():
        tool_rows = db.execute(
            sa.select(tools).where(tools.c.run_id == row["id"])
        ).mappings().all()
        tool_usage = [
            {
                "tool_name": item["tool_name"],
                "call_count": item["call_count"],
                "success_count": item["success_count"],
                "failure_count": item["failure_count"],
                "duration_ms": item["duration_ms"],
            }
            for item in tool_rows
        ]
        db.execute(
            sessions.update()
            .where(sessions.c.id == session_id)
            .values(
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_read_tokens=row["cache_read_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                llm_call_count=row["llm_call_count"],
                tool_usage=tool_usage,
            )
        )


def downgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        for column in (
            "tool_usage",
            "llm_call_count",
            "cache_write_tokens",
            "cache_read_tokens",
            "output_tokens",
            "input_tokens",
        ):
            batch.drop_column(column)