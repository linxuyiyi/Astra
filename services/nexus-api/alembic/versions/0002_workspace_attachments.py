"""增加 Workspace 附件引用，旧附件表由迁移程序核验后清理。"""

import sqlalchemy as sa
from sqlalchemy.dialects.mysql import VARCHAR

from alembic import op

revision = "0002_workspace_attachments"
down_revision = "0001_init"
branch_labels = None
depends_on = None


def identifier(length):
    return sa.String(length).with_variant(VARCHAR(length, collation="utf8mb4_bin"), "mysql")


def upgrade():
    bind = op.get_bind()
    if bind.execute(sa.text(
        "SELECT 1 FROM t_nexus_channel_members m LEFT JOIN t_nexus_messages a "
        "ON a.id=m.last_delivered_message_id AND a.channel_id=m.channel_id "
        "WHERE m.last_delivered_message_id<>'' AND a.seq IS NULL LIMIT 1"
    )).first():
        raise RuntimeError("群成员的旧送达游标缺少对应消息，迁移前须核对该游标")
    op.add_column(
        "t_nexus_channel_members",
        sa.Column("last_delivered_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    bind.execute(sa.text(
        "UPDATE t_nexus_channel_members SET last_delivered_seq=("
        "SELECT a.seq FROM t_nexus_messages a WHERE a.id=t_nexus_channel_members.last_delivered_message_id"
        ") WHERE last_delivered_message_id<>''"
    ))
    op.drop_column("t_nexus_channel_members", "last_delivered_message_id")
    op.create_table(
        "t_nexus_attachment_refs",
        sa.Column("id", identifier(64), primary_key=True),
        sa.Column("owner_uid", identifier(160), nullable=False),
        sa.Column("scope_kind", identifier(16), nullable=False),
        sa.Column("scope_id", identifier(64), nullable=False),
        sa.Column("agent_id", identifier(255), nullable=False),
        sa.Column("session_id", identifier(255), nullable=False),
        sa.Column("name", identifier(255), nullable=False),
        sa.Column("media_type", identifier(160), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", identifier(64), nullable=False),
        sa.Column("linked", sa.Integer(), nullable=False),
        sa.Column("created_at", identifier(40), nullable=False),
    )

    op.add_column(
        "t_nexus_resource_sync",
        sa.Column("mounted_name", identifier(255), nullable=False, server_default=sa.text("''")),
    )
    bind.execute(sa.text("UPDATE t_nexus_resource_sync SET status='pending' WHERE status='applied'"))
    if not bind.execute(sa.text("SELECT 1 FROM t_nexus_attachments LIMIT 1")).first():
        op.drop_table("t_nexus_attachments")


def downgrade():
    raise RuntimeError("降级会丢失 Workspace 附件引用，请恢复升级前的数据库备份")
