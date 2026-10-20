"""Runtime session result index and runtime context binding.

Phase 1A of the session workspace model: RuntimeSession owns the durable
execution result status, and KnowledgeItem can point at the session that
produced an experience. Run/Evidence/legacy tables are untouched.
"""

from alembic import op
import sqlalchemy as sa

revision = "0021_runtime_session_state"
down_revision = "0020_session_workspace"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.add_column(
            sa.Column("status", sa.String(32), nullable=False, server_default="UNKNOWN")
        )
        batch.add_column(sa.Column("terminal_reason", sa.String(64), nullable=True))
        batch.add_column(sa.Column("finish_reason", sa.String(32), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_message_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("model_name", sa.String(160), nullable=True))
        batch.add_column(sa.Column("total_tokens", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("tool_call_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("superseded_by", sa.String(160), nullable=True))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("runtime_context_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("runtime_context_id", sa.String(64), nullable=True))
        batch.create_index("ix_runtime_sessions_status", ["status"])
        batch.create_index("ix_runtime_sessions_superseded_by", ["superseded_by"])
        batch.create_unique_constraint(
            "uq_runtime_sessions_context_hash", ["runtime_context_hash"]
        )
        batch.create_foreign_key(
            "fk_runtime_sessions_superseded_by",
            "runtime_sessions",
            ["superseded_by"],
            ["id"],
        )

    with op.batch_alter_table("knowledge_items") as batch:
        batch.add_column(sa.Column("source_session_id", sa.String(160), nullable=True))
        batch.create_index("ix_knowledge_items_source_session_id", ["source_session_id"])
        batch.create_foreign_key(
            "fk_knowledge_items_source_session_id",
            "runtime_sessions",
            ["source_session_id"],
            ["id"],
            ondelete="SET NULL",
        )

    db = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=db)
    sessions = sa.Table("runtime_sessions", metadata, autoload_with=db)

    status_mapping = {
        "COMPLETED": ("SUCCEEDED", "completed"),
        "FAILED": ("FAILED", None),
        "CANCELLED": ("INTERRUPTED", "aborted"),
        "TIMEOUT": ("INTERRUPTED", "timeout"),
        "WAITING_INPUT": ("WAITING", None),
        "RUNNING": ("UNKNOWN", "transport_unknown"),
        "STARTING": ("UNKNOWN", "transport_unknown"),
        "QUEUED": ("UNKNOWN", "transport_unknown"),
        "SUSPENDED": ("INTERRUPTED", "incomplete"),
        "LOST": ("UNKNOWN", "transport_unknown"),
    }

    # A RuntimeSession may have appeared in more than one legacy Run when the
    # same OpenCode Session was continued. The latest Run is the best final
    # snapshot and avoids double-counting cumulative usage.
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
        status, terminal_reason = status_mapping.get(
            row["status"], ("UNKNOWN", "transport_unknown")
        )
        db.execute(
            sessions.update()
            .where(sessions.c.id == session_id)
            .values(
                status=status,
                terminal_reason=terminal_reason,
                started_at=row["started_at"] or row["created_at"],
                finished_at=row["finished_at"],
                model_name=row["model_name"],
                total_tokens=row["total_tokens"],
                tool_call_count=row["tool_call_count"],
                last_activity_at=row["finished_at"] or row["heartbeat_at"] or row["started_at"],
            )
        )

    # Bindings not backed by a legacy Run remain explicitly unknown.
    db.execute(
        sessions.update()
        .where(sessions.c.started_at.is_(None))
        .values(status="UNKNOWN", started_at=sessions.c.created_at)
    )


def downgrade():
    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_constraint("fk_knowledge_items_source_session_id", type_="foreignkey")
        batch.drop_index("ix_knowledge_items_source_session_id")
        batch.drop_column("source_session_id")

    with op.batch_alter_table("runtime_sessions") as batch:
        batch.drop_constraint("fk_runtime_sessions_superseded_by", type_="foreignkey")
        batch.drop_constraint("uq_runtime_sessions_context_hash", type_="unique")
        batch.drop_index("ix_runtime_sessions_superseded_by")
        batch.drop_index("ix_runtime_sessions_status")
        for column in (
            "runtime_context_id",
            "runtime_context_hash",
            "superseded_at",
            "superseded_by",
            "tool_call_count",
            "total_tokens",
            "model_name",
            "last_activity_at",
            "last_message_id",
            "finished_at",
            "started_at",
            "finish_reason",
            "terminal_reason",
            "status",
        ):
            batch.drop_column(column)