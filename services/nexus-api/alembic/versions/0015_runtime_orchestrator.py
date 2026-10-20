"""Add active Run leases, transactional dispatch outbox and watchdog metadata."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import context, op

revision = "0015_runtime_orchestrator"
down_revision = "0014_runtime_phase1"
branch_labels = None
depends_on = None

ACTIVE_RUN_STATUSES = ("QUEUED", "STARTING", "RUNNING", "WAITING_INPUT", "BLOCKED")


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("worker_id", sa.String(160), nullable=True))
        batch_op.add_column(sa.Column("execution_token", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("executor_status", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("failure_type", sa.String(64), nullable=True))
        batch_op.create_unique_constraint("uq_runs_execution_token", ["execution_token"])
        batch_op.create_index("ix_runs_heartbeat_at", ["heartbeat_at"])
        batch_op.create_index("ix_runs_worker_id", ["worker_id"])
        batch_op.create_index("ix_runs_failure_type", ["failure_type"])
        batch_op.create_index("ix_runs_task_active", ["task_id", "status"])
        batch_op.create_index("ix_runs_employee_active", ["employee_id", "status"])

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("active_run_id", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            "fk_tasks_active_run_id_runs",
            "runs",
            ["active_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_tasks_active_run_id", ["active_run_id"], unique=True)

    with op.batch_alter_table("digital_employees") as batch_op:
        batch_op.add_column(sa.Column("active_run_id", sa.String(64), nullable=True))
        batch_op.create_foreign_key(
            "fk_digital_employees_active_run_id_runs",
            "runs",
            ["active_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index(
            "ix_digital_employees_active_run_id", ["active_run_id"], unique=True
        )

    op.create_table(
        "run_dispatch_outbox",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False, server_default="EXECUTE_RUN"),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_run_dispatch_outbox_run_id", "run_dispatch_outbox", ["run_id"], unique=True)
    op.create_index("ix_run_dispatch_outbox_status", "run_dispatch_outbox", ["status"])
    op.create_index(
        "ix_run_dispatch_outbox_next_retry_at", "run_dispatch_outbox", ["next_retry_at"]
    )

    # Offline SQL generation cannot inspect existing rows. The schema portion is
    # still emitted; live upgrades perform the safe lease/outbox data backfill.
    if context.is_offline_mode():
        return

    bind = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)
    placeholders = ", ".join(f"'{status}'" for status in ACTIVE_RUN_STATUSES)
    active_rows = bind.execute(
        sa.text(
            "SELECT id, task_id, employee_id, status FROM runs "
            f"WHERE status IN ({placeholders}) ORDER BY run_number DESC, created_at DESC"
        )
    ).mappings()
    claimed_tasks: set[str] = set()
    claimed_employees: set[str] = set()
    queued_runs: list[str] = []
    for row in active_rows:
        run_id = str(row["id"])
        task_id = str(row["task_id"])
        employee_id = str(row["employee_id"])
        if task_id in claimed_tasks or employee_id in claimed_employees:
            bind.execute(
                sa.text(
                    "UPDATE runs SET status = 'LOST', finished_at = :now, "
                    "heartbeat_at = :now, executor_status = 'LOST', "
                    "failure_type = 'MIGRATION_LEASE_CONFLICT' WHERE id = :run_id"
                ),
                {"now": now, "run_id": run_id},
            )
            continue
        if row["status"] == "QUEUED":
            queued_runs.append(run_id)
        bind.execute(
            sa.text("UPDATE tasks SET active_run_id = :run_id WHERE id = :task_id"),
            {"run_id": run_id, "task_id": task_id},
        )
        bind.execute(
            sa.text(
                "UPDATE digital_employees SET active_run_id = :run_id WHERE id = :employee_id"
            ),
            {"run_id": run_id, "employee_id": employee_id},
        )
        claimed_tasks.add(task_id)
        claimed_employees.add(employee_id)

    bind.execute(
        sa.text(
            "UPDATE runs SET heartbeat_at = COALESCE(heartbeat_at, started_at, created_at) "
            "WHERE status IN ('STARTING', 'RUNNING')"
        )
    )
    outbox = sa.table(
        "run_dispatch_outbox",
        sa.column("id", sa.String),
        sa.column("run_id", sa.String),
        sa.column("event_type", sa.String),
        sa.column("status", sa.String),
        sa.column("attempts", sa.Integer),
        sa.column("next_retry_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    if queued_runs:
        op.bulk_insert(
            outbox,
            [
                {
                    "id": f"OUT-{uuid4().hex[:12].upper()}",
                    "run_id": run_id,
                    "event_type": "EXECUTE_RUN",
                    "status": "PENDING",
                    "attempts": 0,
                    "next_retry_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                for run_id in dict.fromkeys(queued_runs)
            ],
        )


def downgrade() -> None:
    op.drop_index("ix_run_dispatch_outbox_next_retry_at", table_name="run_dispatch_outbox")
    op.drop_index("ix_run_dispatch_outbox_status", table_name="run_dispatch_outbox")
    op.drop_index("ix_run_dispatch_outbox_run_id", table_name="run_dispatch_outbox")
    op.drop_table("run_dispatch_outbox")

    with op.batch_alter_table("digital_employees") as batch_op:
        batch_op.drop_index("ix_digital_employees_active_run_id")
        batch_op.drop_constraint(
            "fk_digital_employees_active_run_id_runs", type_="foreignkey"
        )
        batch_op.drop_column("active_run_id")

    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_index("ix_tasks_active_run_id")
        batch_op.drop_constraint("fk_tasks_active_run_id_runs", type_="foreignkey")
        batch_op.drop_column("active_run_id")

    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_employee_active")
        batch_op.drop_index("ix_runs_task_active")
        batch_op.drop_index("ix_runs_failure_type")
        batch_op.drop_index("ix_runs_worker_id")
        batch_op.drop_index("ix_runs_heartbeat_at")
        batch_op.drop_constraint("uq_runs_execution_token", type_="unique")
        batch_op.drop_column("failure_type")
        batch_op.drop_column("executor_status")
        batch_op.drop_column("execution_token")
        batch_op.drop_column("worker_id")
        batch_op.drop_column("heartbeat_at")
