"""Add explicit queued Run delivery metadata for runtime Phase 1."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_runtime_phase1"
down_revision = "0013_assistant"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("previous_run_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("queued_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("dispatched_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("celery_task_id", sa.String(160), nullable=True))
        batch_op.add_column(
            sa.Column("dispatch_attempt", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_runs_previous_run_id_runs",
            "runs",
            ["previous_run_id"],
            ["id"],
        )
        batch_op.create_index("ix_runs_previous_run_id", ["previous_run_id"])
        batch_op.create_index("ix_runs_queued_at", ["queued_at"])
        batch_op.create_index("ix_runs_dispatched_at", ["dispatched_at"])
        batch_op.create_index("ix_runs_celery_task_id", ["celery_task_id"])


def downgrade() -> None:
    runs = sa.table(
        "runs",
        sa.column("started_at", sa.DateTime),
        sa.column("queued_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
    )
    op.execute(
        runs.update()
        .where(runs.c.started_at.is_(None))
        .values(started_at=sa.func.coalesce(runs.c.queued_at, runs.c.created_at))
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_index("ix_runs_celery_task_id")
        batch_op.drop_index("ix_runs_dispatched_at")
        batch_op.drop_index("ix_runs_queued_at")
        batch_op.drop_index("ix_runs_previous_run_id")
        batch_op.drop_constraint("fk_runs_previous_run_id_runs", type_="foreignkey")
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
        batch_op.drop_column("dispatch_attempt")
        batch_op.drop_column("celery_task_id")
        batch_op.drop_column("dispatched_at")
        batch_op.drop_column("queued_at")
        batch_op.drop_column("previous_run_id")
