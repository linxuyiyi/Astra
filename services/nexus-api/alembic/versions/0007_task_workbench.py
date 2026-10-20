"""Add durable task workbench sessions, messages and inquiries."""

import sqlalchemy as sa

from alembic import op

revision = "0007_task_workbench"
down_revision = "0006_ssh_password"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.Column("current_run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_task_sessions_task_id"),
    )
    op.create_index("ix_task_sessions_task_id", "task_sessions", ["task_id"])
    op.create_index("ix_task_sessions_status", "task_sessions", ["status"])
    op.create_index("ix_task_sessions_current_run_id", "task_sessions", ["current_run_id"])

    op.create_table(
        "session_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("task_sessions.id"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_session_messages_session_id", "session_messages", ["session_id"])
    op.create_index("ix_session_messages_run_id", "session_messages", ["run_id"])
    op.create_index("ix_session_messages_role", "session_messages", ["role"])
    op.create_index("ix_session_messages_status", "session_messages", ["status"])
    op.create_index("ix_session_messages_created_at", "session_messages", ["created_at"])

    op.create_table(
        "run_inquiries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("task_sessions.id"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("agent_name", sa.String(160), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("options", sa.JSON(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("answered_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_run_inquiries_session_id", "run_inquiries", ["session_id"])
    op.create_index("ix_run_inquiries_run_id", "run_inquiries", ["run_id"])
    op.create_index("ix_run_inquiries_status", "run_inquiries", ["status"])
    op.create_index("ix_run_inquiries_created_at", "run_inquiries", ["created_at"])


def downgrade() -> None:
    op.drop_table("run_inquiries")
    op.drop_table("session_messages")
    op.drop_table("task_sessions")
