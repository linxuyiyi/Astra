"""Agent Worker 维护操作与可续传事件。

维护状态不再写进环境 status_detail.operation。活动环境唯一约束保证同一环境
只有一个未终态维护；downgrade 删除两张表。
"""

import sqlalchemy as sa

from alembic import op

revision = "0048_worker_maintenance"
down_revision = "0047_knowledge_agentscope"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "worker_instances",
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
    )
    op.create_table(
        "worker_maintenance_operations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=64), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.String(length=120), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_owner", sa.String(length=120), nullable=True),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("danger_dispatched", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("reconcile_required", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("reconcile_only", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("target_revision", sa.JSON(), nullable=False),
        sa.Column("before_snapshot", sa.JSON(), nullable=False),
        sa.Column("after_snapshot", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("execution_channel", sa.String(length=16), nullable=True),
        sa.Column("execution_reason", sa.Text(), nullable=True),
        sa.Column("retry_of_attempt", sa.Integer(), nullable=True),
        sa.Column("active_environment_id", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["opencode_execution_environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "active_environment_id", name="uq_worker_maintenance_active_environment"
        ),
    )
    op.create_index(
        "ix_worker_maintenance_operations_environment_id",
        "worker_maintenance_operations",
        ["environment_id"],
    )
    op.create_index(
        "ix_worker_maintenance_operations_state",
        "worker_maintenance_operations",
        ["state"],
    )
    op.create_index(
        "ix_worker_maintenance_operations_lease_owner",
        "worker_maintenance_operations",
        ["lease_owner"],
    )
    op.create_index(
        "ix_worker_maintenance_state_lease",
        "worker_maintenance_operations",
        ["state", "lease_until"],
    )

    op.create_table(
        "worker_maintenance_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("type", sa.String(length=32), nullable=False, server_default="step"),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["operation_id"], ["worker_maintenance_operations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_worker_maintenance_events_operation_id",
        "worker_maintenance_events",
        ["operation_id"],
    )
    op.create_index(
        "ix_worker_maintenance_events_created_at",
        "worker_maintenance_events",
        ["created_at"],
    )


def downgrade():
    op.drop_index(
        "ix_worker_maintenance_events_created_at", table_name="worker_maintenance_events"
    )
    op.drop_index(
        "ix_worker_maintenance_events_operation_id", table_name="worker_maintenance_events"
    )
    op.drop_table("worker_maintenance_events")
    op.drop_index("ix_worker_maintenance_state_lease", table_name="worker_maintenance_operations")
    op.drop_index(
        "ix_worker_maintenance_operations_lease_owner",
        table_name="worker_maintenance_operations",
    )
    op.drop_index(
        "ix_worker_maintenance_operations_state", table_name="worker_maintenance_operations"
    )
    op.drop_index(
        "ix_worker_maintenance_operations_environment_id",
        table_name="worker_maintenance_operations",
    )
    op.drop_table("worker_maintenance_operations")
    op.drop_column("worker_instances", "token_ciphertext")
