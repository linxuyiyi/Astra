"""调用日志记录处理实例代次，并把失联实例上的未完成请求标成结果未知。

新请求写入 gateway_instance_id 与 gateway_instance_started_at。启动恢复和周期扫描只把
「实例已不在对应代次的 ACTIVE 心跳上」的未完成行标为 outcome=lost，并写入 finished_at。
不填写 status_code 或 error_code，因此不会被看成 500 或成功。

历史行（finished_at 为空且 gateway_instance_id 为空）保持原样。统计单独计为
legacy_open，查询路径不按年龄猜测成败。运维可以只读列出这些行再决定是否人工处理。

保留清理只应删除 finished_at 已写入的终态历史（正常结束与 lost）。进行中和历史未完成不删。

downgrade 删除这三列，不删除 gateway_runtime_events。
"""

import sqlalchemy as sa

from alembic import op

revision = "0038_gateway_request_lifecycle"
down_revision = "0037_gateway_runtime_events"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "gateway_requests",
        sa.Column("gateway_instance_id", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("gateway_instance_started_at", sa.DateTime(), nullable=True),
    )
    op.add_column("gateway_requests", sa.Column("outcome", sa.String(length=16), nullable=True))
    op.create_index("ix_gateway_requests_instance", "gateway_requests", ["gateway_instance_id"])


def downgrade():
    op.drop_index("ix_gateway_requests_instance", table_name="gateway_requests")
    op.drop_column("gateway_requests", "outcome")
    op.drop_column("gateway_requests", "gateway_instance_started_at")
    op.drop_column("gateway_requests", "gateway_instance_id")
