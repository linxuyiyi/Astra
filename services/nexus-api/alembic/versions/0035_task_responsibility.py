"""任务责任员工、来源会话与唤醒 outbox。"""

import sqlalchemy as sa

from alembic import op

revision = "0035_task_responsibility"
down_revision = "0034_gateway_key_deletion"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("responsible_employee_id", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("source_session_id", sa.String(length=160), nullable=True))
    op.add_column("tasks", sa.Column("source_conversation_id", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("source_channel_id", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("idempotency_key", sa.String(length=160), nullable=True))
    op.create_index("ix_tasks_responsible_employee_id", "tasks", ["responsible_employee_id"])
    op.create_index("ix_tasks_source_channel_id", "tasks", ["source_channel_id"])
    op.create_index("ix_tasks_idempotency_key", "tasks", ["idempotency_key"], unique=True)
    op.create_table(
        "task_wake_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("responsible_employee_id", sa.String(length=64), nullable=False),
        sa.Column("source_session_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("hint", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("event_id", name="uq_task_wake_outbox_event_id"),
    )
    op.create_index("ix_task_wake_outbox_task_id", "task_wake_outbox", ["task_id"])
    op.create_index("ix_task_wake_outbox_status", "task_wake_outbox", ["status"])
    op.create_index("ix_task_wake_outbox_next_retry_at", "task_wake_outbox", ["next_retry_at"])


def downgrade():
    op.drop_index("ix_task_wake_outbox_next_retry_at", table_name="task_wake_outbox")
    op.drop_index("ix_task_wake_outbox_status", table_name="task_wake_outbox")
    op.drop_index("ix_task_wake_outbox_task_id", table_name="task_wake_outbox")
    op.drop_table("task_wake_outbox")
    op.drop_index("ix_tasks_idempotency_key", table_name="tasks")
    op.drop_index("ix_tasks_source_channel_id", table_name="tasks")
    op.drop_index("ix_tasks_responsible_employee_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("idempotency_key")
        batch.drop_column("source_channel_id")
        batch.drop_column("source_conversation_id")
        batch.drop_column("source_session_id")
        batch.drop_column("responsible_employee_id")
