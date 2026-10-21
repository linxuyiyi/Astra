"""Add the default node pool and normalize node weights to positive values."""

import sqlalchemy as sa

from alembic import op

revision = "0057_gateway_default_pool_and_node_weight"
down_revision = "0056_gateway_probe_strategy"
branch_labels = None
depends_on = None

DEFAULT_POOL_ID = "gp_default"
DEFAULT_POOL_NAME = "默认资源池"


def upgrade():
    # 历史零值/空权重在度量为 1，迁移后节点权重必须为正数。
    op.execute("UPDATE t_nexus_gateway_nodes SET weight = 1 WHERE weight IS NULL OR weight < 1")
    bind = op.get_bind()
    by_id = bind.execute(
        sa.text("SELECT 1 FROM t_nexus_gateway_pools WHERE id = :pool_id"),
        {"pool_id": DEFAULT_POOL_ID},
    ).first()
    by_name = bind.execute(
        sa.text("SELECT 1 FROM t_nexus_gateway_pools WHERE name = :name"),
        {"name": DEFAULT_POOL_NAME},
    ).first()
    # 只补一个可选的默认归属，不改现有节点归属，也不接纳未分配节点。
    if by_id is None and by_name is None:
        bind.execute(
            sa.text(
                "INSERT INTO t_nexus_gateway_pools "
                "(id, name, description, weight, enabled, sort_order, created_at, updated_at) "
                "VALUES (:pool_id, :name, :description, 1, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "pool_id": DEFAULT_POOL_ID,
                "name": DEFAULT_POOL_NAME,
                "description": "系统默认资源池；不自动接纳未分配节点",
            },
        )


def downgrade():
    op.get_bind().execute(
        sa.text(
            "DELETE FROM t_nexus_gateway_pools WHERE id = :pool_id AND NOT EXISTS ("
            "SELECT 1 FROM t_nexus_gateway_nodes WHERE pool_id = :pool_id)"
        ),
        {"pool_id": DEFAULT_POOL_ID},
    )
