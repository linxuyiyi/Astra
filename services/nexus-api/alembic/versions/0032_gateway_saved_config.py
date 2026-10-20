"""删除 API 中转配置版本历史，已保存配置直接成为数据面事实源。

保留节点、资源池、路由、Key、吊销记录、健康记录与审计；不将历史快照
覆盖到当前配置表。删除的版本历史只能通过备份恢复。
"""

from alembic import op

revision = "0032_gateway_saved_config"
down_revision = "0031_api_gateway_control"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("gateway_config_versions")


def downgrade():
    raise RuntimeError("配置版本历史删除不可逆，请恢复升级前的数据库备份")
