"""Remove the retired platform assistant and its task review/summary data.

The platform assistant was replaced by digital employees. Its OpenCode runtime,
MCP server, conversations, prepared operations, task reviews and task summaries
are no longer part of Nexus, so this migration removes the tables and the dead
``review_history`` column from ``t_nexus_tasks``.

This is a destructive removal: downgrade recreates the empty schema at 0052
but cannot restore deleted rows. Keep a pre-0053 backup when data may be needed.
"""

import sqlalchemy as sa

from alembic import op

revision = "0053_remove_platform_assistant"
down_revision = "0052_t_nexus_table_prefix"
branch_labels = None
depends_on = None

ASSISTANT_TABLES = (
    "t_nexus_assistant_tool_calls",
    "t_nexus_assistant_operations",
    "t_nexus_assistant_context_refs",
    "t_nexus_assistant_message_parts",
    "t_nexus_assistant_messages",
    "t_nexus_assistant_conversations",
)


def upgrade():
    for table in ASSISTANT_TABLES:
        op.drop_table(table)
    op.drop_table("t_nexus_task_reviews")
    op.drop_table("t_nexus_task_summaries")
    with op.batch_alter_table("t_nexus_tasks") as batch:
        batch.drop_column("review_history")


def downgrade():
    """Recreate empty tables so older revisions can still be reached.

    Data is not restored: the removal is destructive and the platform assistant
    is gone. This shape matches revision 0052 so the 0052 downgrade can rename
    every table back to its historical name.
    """

    with op.batch_alter_table("t_nexus_tasks") as batch:
        batch.add_column(sa.Column("review_history", sa.JSON(), nullable=False, server_default="[]"))

    op.create_table(
        "t_nexus_task_summaries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("primary_session_id", sa.String(160), nullable=True),
        sa.Column("referenced_session_ids", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("acceptance_results", sa.JSON(), nullable=False),
        sa.Column("key_changes", sa.JSON(), nullable=False),
        sa.Column("source_status", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("generated_by", sa.String(120), nullable=False),
        sa.Column("source_ref_snapshot", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["primary_session_id"],
            ["t_nexus_runtime_sessions.id"],
            name="fk_task_summaries_primary_session_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_task_summaries_task_id", "t_nexus_task_summaries", ["task_id"], unique=True)
    op.create_index("ix_task_summaries_stale", "t_nexus_task_summaries", ["stale"])

    op.create_table(
        "t_nexus_task_reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "task_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_session_id",
            sa.String(160),
            sa.ForeignKey("t_nexus_runtime_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_status", sa.String(32), nullable=False),
        sa.Column("turn_anchor_id", sa.String(160)),
        sa.Column("final_message_id", sa.String(160)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("assistant_session_id", sa.String(160)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("retry_at", sa.DateTime(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("result", sa.JSON(), nullable=False),
    )
    op.create_index("ix_task_reviews_task_id", "t_nexus_task_reviews", ["task_id"])

    op.create_table(
        "t_nexus_assistant_conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(120), nullable=False),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("opencode_session_id", sa.String(160), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "t_nexus_assistant_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_assistant_conversations.id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("opencode_message_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "t_nexus_assistant_message_parts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "message_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_assistant_messages.id"),
            nullable=False,
        ),
        sa.Column("part_type", sa.String(32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("message_id", "seq", name="uq_assistant_part_seq"),
    )
    op.create_table(
        "t_nexus_assistant_context_refs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_assistant_conversations.id"),
            nullable=False,
        ),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("ref_type", sa.String(32), nullable=False),
        sa.Column("ref_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "t_nexus_assistant_operations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_assistant_conversations.id"),
            nullable=True,
        ),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(120), nullable=False),
        sa.Column("operation_type", sa.String(48), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_assistant_operation_conversation",
        "t_nexus_assistant_operations",
        ["conversation_id", "status"],
    )
    op.create_table(
        "t_nexus_assistant_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("opencode_session_id", sa.String(160), nullable=True),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
