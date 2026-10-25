"""删除误引入的本地账号系统；业务归属 ID 不变。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0004_remove_local_auth"
down_revision = "0003_gateway_speech"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "t_nexus_identity_profiles",
        sa.Column(
            "id",
            sa.String(64).with_variant(mysql.VARCHAR(64, collation="utf8mb4_bin"), "mysql"),
            primary_key=True,
        ),
        sa.Column(
            "display_name",
            sa.String(64).with_variant(mysql.VARCHAR(64, collation="utf8mb4_bin"), "mysql"),
            nullable=False,
        ),
    )
    # 临时 UID 目录不复制旧账号，已有群成员与 owner ID 保持原值。
    op.drop_table("t_nexus_auth_sessions")
    op.drop_table("t_nexus_users")


def downgrade():
    raise RuntimeError("本地认证已移除，密码和会话不可恢复；请从备份恢复")
