"""已发布知识项到 AgentScope knowledge base 的映射。不改 LLM Wiki 的同步列。"""

import sqlalchemy as sa

from alembic import op

revision = "0047_knowledge_agentscope"
down_revision = "0046_task_waiting_reason"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_items",
        sa.Column("agentscope_kb_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("agentscope_document_id", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "knowledge_items",
        sa.Column(
            "agentscope_sync_status",
            sa.String(length=16),
            nullable=False,
            server_default="not_synced",
        ),
    )
    op.add_column("knowledge_items", sa.Column("agentscope_sync_error", sa.Text(), nullable=True))
    op.create_index("ix_knowledge_items_agentscope_kb_id", "knowledge_items", ["agentscope_kb_id"])


def downgrade():
    op.drop_index("ix_knowledge_items_agentscope_kb_id", table_name="knowledge_items")
    op.drop_column("knowledge_items", "agentscope_sync_error")
    op.drop_column("knowledge_items", "agentscope_sync_status")
    op.drop_column("knowledge_items", "agentscope_document_id")
    op.drop_column("knowledge_items", "agentscope_kb_id")
