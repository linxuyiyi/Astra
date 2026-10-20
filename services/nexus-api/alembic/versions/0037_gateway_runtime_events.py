"""记录 API 中转健康态变迁，供运行总览事件流使用。

配置变更仍在 gateway_audit_events。探测明细仍在 gateway_health_checks。
本表只在健康态真正变化时追加一行。
"""

import sqlalchemy as sa

from alembic import op

revision = "0037_gateway_runtime_events"
down_revision = "0036_experience_space_and_key_last_used"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gateway_runtime_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(length=16), nullable=False, server_default="runtime"),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=True),
        sa.Column("pool_id", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("summary", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("previous", sa.String(length=32), nullable=True),
        sa.Column("current", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gateway_runtime_events_source", "gateway_runtime_events", ["source"])
    op.create_index("ix_gateway_runtime_events_kind", "gateway_runtime_events", ["kind"])
    op.create_index("ix_gateway_runtime_events_node_id", "gateway_runtime_events", ["node_id"])
    op.create_index(
        "ix_gateway_runtime_events_created_at", "gateway_runtime_events", ["created_at"]
    )
    op.create_index(
        "ix_gateway_runtime_node_time",
        "gateway_runtime_events",
        ["node_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_gateway_runtime_node_time", table_name="gateway_runtime_events")
    op.drop_index("ix_gateway_runtime_events_created_at", table_name="gateway_runtime_events")
    op.drop_index("ix_gateway_runtime_events_node_id", table_name="gateway_runtime_events")
    op.drop_index("ix_gateway_runtime_events_kind", table_name="gateway_runtime_events")
    op.drop_index("ix_gateway_runtime_events_source", table_name="gateway_runtime_events")
    op.drop_table("gateway_runtime_events")
