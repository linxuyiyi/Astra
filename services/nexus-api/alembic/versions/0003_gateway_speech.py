"""网关模型类型与语音输入路由；接在 collaborative_groups 之后。"""

import sqlalchemy as sa

from alembic import op

revision = "0003_gateway_speech"
down_revision = "0002_collaborative_groups"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "t_nexus_gateway_routes",
        sa.Column("model_type", sa.String(16), nullable=False, server_default="CHAT"),
    )
    # 只按明确的纯嵌入节点配置分类，混合池保持原始 CHAT，交管理员核对。
    op.execute(sa.text("""
        UPDATE t_nexus_gateway_routes SET model_type = 'EMBEDDING'
        WHERE pool_id IN (
            SELECT pool_id FROM t_nexus_gateway_nodes WHERE pool_id IS NOT NULL
            GROUP BY pool_id
            HAVING COUNT(*) = SUM(CASE WHEN probe_strategy = 'EMBEDDING' THEN 1 ELSE 0 END)
        )
    """))
    op.create_table(
        "t_nexus_gateway_speech_input",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.Column("route_id", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(
            ["route_id"], ["t_nexus_gateway_routes.id"], ondelete="SET NULL"
        ),
    )


def downgrade():
    op.drop_table("t_nexus_gateway_speech_input")
    op.drop_column("t_nexus_gateway_routes", "model_type")
