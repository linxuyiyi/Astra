"""Astra V0.1 initial schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001_astra_v01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digital_employees",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("source_scope", sa.JSON(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("current_task_count", sa.Integer(), nullable=False),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True)),
        sa.Column("online_since", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_digital_employees_status", "digital_employees", ["status"])
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("external_task_id", sa.String(160)),
        sa.Column("task_type", sa.String(80), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("definition_of_done", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(8), nullable=False),
        sa.Column("priority_weight", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id")),
        sa.Column("workspace", sa.Text()),
        sa.Column("opencode_session_id", sa.String(160)),
        sa.Column("current_phase", sa.String(80), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retry_count", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("human_required_reason", sa.Text()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "external_task_id", name="uq_task_source_external"),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_task_type", "tasks", ["task_type"])
    op.create_index("ix_tasks_dispatch", "tasks", ["status", "priority_weight", "created_at"])
    op.create_table(
        "runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id"), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("executor_type", sa.String(64), nullable=False),
        sa.Column("opencode_session_id", sa.String(160)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "run_number", name="uq_task_run_number"),
    )
    op.create_index("ix_runs_task_id", "runs", ["task_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id")),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id")),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id")),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("phase", sa.String(80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_events_task_id", "events", ["task_id"])
    op.create_index("ix_events_event_type", "events", ["event_type"])
    op.create_index("ix_events_created_at", "events", ["created_at"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id")),
        sa.Column("name", sa.String(180), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(120), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_task_id", "evidence", ["task_id"])


def downgrade() -> None:
    op.drop_table("evidence")
    op.drop_table("events")
    op.drop_table("runs")
    op.drop_table("tasks")
    op.drop_table("digital_employees")
