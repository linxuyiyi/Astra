"""Add NEXUS platform assistant tables."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0013_assistant"
down_revision = "0012_employee_default_work_area"
branch_labels = None
depends_on = None


ASSISTANT_TABLE_COLUMNS = {
    "assistant_conversations": {
        "id",
        "user_id",
        "title",
        "opencode_session_id",
        "status",
        "last_message_at",
        "created_at",
        "updated_at",
    },
    "assistant_messages": {
        "id",
        "conversation_id",
        "role",
        "message_type",
        "content",
        "status",
        "opencode_message_id",
        "created_at",
        "updated_at",
    },
    "assistant_message_parts": {"id", "message_id", "part_type", "seq", "payload", "created_at"},
    "assistant_context_refs": {
        "id",
        "conversation_id",
        "message_id",
        "ref_type",
        "ref_id",
        "created_at",
    },
    "assistant_operations": {
        "id",
        "operation_id",
        "conversation_id",
        "message_id",
        "user_id",
        "operation_type",
        "risk_level",
        "status",
        "request_json",
        "preview_json",
        "result_json",
        "expires_at",
        "confirmed_at",
        "executed_at",
        "created_at",
        "updated_at",
    },
    "assistant_tool_calls": {
        "id",
        "conversation_id",
        "message_id",
        "opencode_session_id",
        "tool_name",
        "arguments_json",
        "result_json",
        "status",
        "duration_ms",
        "created_at",
    },
}

ASSISTANT_TABLE_INDEXES = {
    "assistant_conversations": {
        "ix_assistant_conversations_user_id",
        "ix_assistant_conversations_opencode_session_id",
        "ix_assistant_conversations_status",
    },
    "assistant_messages": {
        "ix_assistant_messages_conversation_id",
        "ix_assistant_messages_role",
        "ix_assistant_messages_status",
        "ix_assistant_messages_created_at",
    },
    "assistant_message_parts": {
        "ix_assistant_message_parts_message_id",
        "ix_assistant_message_parts_part_type",
    },
    "assistant_context_refs": {
        "ix_assistant_context_refs_conversation_id",
        "ix_assistant_context_refs_ref_type",
        "ix_assistant_context_refs_ref_id",
        "ix_assistant_context_refs_created_at",
    },
    "assistant_operations": {
        "ix_assistant_operations_operation_id",
        "ix_assistant_operations_user_id",
        "ix_assistant_operations_operation_type",
        "ix_assistant_operations_status",
        "ix_assistant_operations_created_at",
        "ix_assistant_operation_conversation",
    },
    "assistant_tool_calls": {
        "ix_assistant_tool_calls_conversation_id",
        "ix_assistant_tool_calls_tool_name",
        "ix_assistant_tool_calls_status",
        "ix_assistant_tool_calls_created_at",
    },
}


def _adopt_precreated_schema() -> bool:
    """Adopt the exact schema that an earlier development startup created.

    Historical local startup calls ``Base.metadata.create_all``. A developer may
    therefore already have all assistant tables while Alembic still reports
    revision 0012. Preserve those tables and their data, but reject incomplete or
    structurally different schemas instead of silently stamping them as current.
    """

    if context.is_offline_mode():
        return False

    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    present = existing.intersection(ASSISTANT_TABLE_COLUMNS)
    if not present:
        return False
    if present != set(ASSISTANT_TABLE_COLUMNS):
        missing = sorted(set(ASSISTANT_TABLE_COLUMNS) - present)
        raise RuntimeError(f"检测到不完整的平台助手预建表，缺少: {', '.join(missing)}")

    for table_name, expected_columns in ASSISTANT_TABLE_COLUMNS.items():
        actual_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if actual_columns != expected_columns:
            missing = sorted(expected_columns - actual_columns)
            extra = sorted(actual_columns - expected_columns)
            raise RuntimeError(
                f"平台助手预建表 {table_name} 结构不匹配; "
                f"缺少列: {missing or '无'}; 多余列: {extra or '无'}"
            )
        actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        missing_indexes = sorted(ASSISTANT_TABLE_INDEXES[table_name] - actual_indexes)
        if missing_indexes:
            raise RuntimeError(
                f"平台助手预建表 {table_name} 缺少索引: {', '.join(missing_indexes)}"
            )

    unique_constraints = {
        constraint["name"] for constraint in inspector.get_unique_constraints("assistant_message_parts")
    }
    if "uq_assistant_part_seq" not in unique_constraints:
        raise RuntimeError("平台助手预建表 assistant_message_parts 缺少唯一约束 uq_assistant_part_seq")
    return True


def upgrade() -> None:
    if _adopt_precreated_schema():
        return

    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(120), nullable=False, index=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("opencode_session_id", sa.String(160), nullable=True, index=True),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("last_message_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("role", sa.String(24), nullable=False, index=True),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("opencode_message_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "assistant_message_parts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "message_id",
            sa.String(64),
            sa.ForeignKey("assistant_messages.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("part_type", sa.String(32), nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("message_id", "seq", name="uq_assistant_part_seq"),
    )
    op.create_table(
        "assistant_context_refs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("ref_type", sa.String(32), nullable=False, index=True),
        sa.Column("ref_id", sa.String(120), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "assistant_operations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column(
            "conversation_id",
            sa.String(64),
            sa.ForeignKey("assistant_conversations.id"),
            nullable=True,
        ),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("user_id", sa.String(120), nullable=False, index=True),
        sa.Column("operation_type", sa.String(48), nullable=False, index=True),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_assistant_operation_conversation",
        "assistant_operations",
        ["conversation_id", "status"],
    )
    op.create_table(
        "assistant_tool_calls",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.String(64), nullable=True, index=True),
        sa.Column("message_id", sa.String(64), nullable=True),
        sa.Column("opencode_session_id", sa.String(160), nullable=True),
        sa.Column("tool_name", sa.String(160), nullable=False, index=True),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("assistant_tool_calls")
    op.drop_index("ix_assistant_operation_conversation", table_name="assistant_operations")
    op.drop_table("assistant_operations")
    op.drop_table("assistant_context_refs")
    op.drop_table("assistant_message_parts")
    op.drop_table("assistant_messages")
    op.drop_table("assistant_conversations")
