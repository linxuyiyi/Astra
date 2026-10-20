"""Worker 实例、连接纪元和已持久化的控制指令。

Worker 身份独立于数字员工。token 只存摘要。epoch 单调递增，旧连接行改为 superseded。
指令在投递前写入 worker_commands，并带上投递次数、发布状态和最近投递时间。
downgrade 删除这三张表。
"""

import sqlalchemy as sa

from alembic import op

revision = "0039_worker_gateway"
down_revision = "0038_gateway_request_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_instances",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("token_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("connected_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["opencode_execution_environments.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("environment_id", name="uq_worker_instances_environment"),
    )
    op.create_index("ix_worker_instances_environment_id", "worker_instances", ["environment_id"])
    op.create_index("ix_worker_instances_status", "worker_instances", ["status"])

    op.create_table(
        "worker_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("worker_instance_id", sa.String(length=64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["worker_instance_id"], ["worker_instances.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_worker_connections_worker_instance_id", "worker_connections", ["worker_instance_id"])
    op.create_index("ix_worker_connections_state", "worker_connections", ["state"])
    op.create_index(
        "ix_worker_connections_worker_state",
        "worker_connections",
        ["worker_instance_id", "state"],
    )

    op.create_table(
        "worker_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("worker_instance_id", sa.String(length=64), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=160), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "delivery_state",
            sa.String(length=16),
            nullable=False,
            server_default="unpublished",
        ),
        sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("dispatched_epoch", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["worker_instance_id"], ["worker_instances.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("request_id", name="uq_worker_commands_request_id"),
    )
    op.create_index("ix_worker_commands_request_id", "worker_commands", ["request_id"])
    op.create_index("ix_worker_commands_worker_instance_id", "worker_commands", ["worker_instance_id"])
    op.create_index("ix_worker_commands_environment_id", "worker_commands", ["environment_id"])
    op.create_index("ix_worker_commands_status", "worker_commands", ["status"])
    op.create_index("ix_worker_commands_status_deadline", "worker_commands", ["status", "deadline_at"])


def downgrade():
    op.drop_index("ix_worker_commands_status_deadline", table_name="worker_commands")
    op.drop_index("ix_worker_commands_status", table_name="worker_commands")
    op.drop_index("ix_worker_commands_environment_id", table_name="worker_commands")
    op.drop_index("ix_worker_commands_worker_instance_id", table_name="worker_commands")
    op.drop_index("ix_worker_commands_request_id", table_name="worker_commands")
    op.drop_table("worker_commands")
    op.drop_index("ix_worker_connections_worker_state", table_name="worker_connections")
    op.drop_index("ix_worker_connections_state", table_name="worker_connections")
    op.drop_index("ix_worker_connections_worker_instance_id", table_name="worker_connections")
    op.drop_table("worker_connections")
    op.drop_index("ix_worker_instances_status", table_name="worker_instances")
    op.drop_index("ix_worker_instances_environment_id", table_name="worker_instances")
    op.drop_table("worker_instances")
