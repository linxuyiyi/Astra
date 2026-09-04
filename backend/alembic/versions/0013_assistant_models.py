"""assistant models

Revision ID: 0013_assistant_models
Revises: 0012_employee_default_work_area
Create Date: 2026-09-02 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '0013_assistant_models'
down_revision = '0012_employee_default_work_area'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table('assistant_conversations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=120), nullable=False),
    sa.Column('title', sa.String(length=255), nullable=False),
    sa.Column('opencode_session_id', sa.String(length=128), nullable=True),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('last_message_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_conversations_status'), 'assistant_conversations', ['status'], unique=False)
    op.create_index(op.f('ix_assistant_conversations_user_id'), 'assistant_conversations', ['user_id'], unique=False)

    op.create_table('assistant_messages',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('message_type', sa.String(length=32), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('opencode_message_id', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_messages_conversation_id'), 'assistant_messages', ['conversation_id'], unique=False)

    op.create_table('assistant_context_refs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('message_id', sa.String(length=64), nullable=True),
    sa.Column('ref_type', sa.String(length=64), nullable=False),
    sa.Column('ref_id', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['assistant_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_context_refs_conversation_id'), 'assistant_context_refs', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_assistant_context_refs_message_id'), 'assistant_context_refs', ['message_id'], unique=False)

    op.create_table('assistant_message_parts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('message_id', sa.String(length=64), nullable=False),
    sa.Column('part_type', sa.String(length=32), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('payload_json', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['assistant_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_message_parts_message_id'), 'assistant_message_parts', ['message_id'], unique=False)

    op.create_table('assistant_operations',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('operation_id', sa.String(length=128), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('message_id', sa.String(length=64), nullable=True),
    sa.Column('user_id', sa.String(length=120), nullable=False),
    sa.Column('operation_type', sa.String(length=64), nullable=False),
    sa.Column('risk_level', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('request_json', sa.JSON(), nullable=False),
    sa.Column('preview_json', sa.JSON(), nullable=False),
    sa.Column('result_json', sa.JSON(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('executed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['assistant_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_operations_conversation_id'), 'assistant_operations', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_assistant_operations_message_id'), 'assistant_operations', ['message_id'], unique=False)
    op.create_index(op.f('ix_assistant_operations_operation_id'), 'assistant_operations', ['operation_id'], unique=True)
    op.create_index(op.f('ix_assistant_operations_status'), 'assistant_operations', ['status'], unique=False)
    op.create_index(op.f('ix_assistant_operations_user_id'), 'assistant_operations', ['user_id'], unique=False)

    op.create_table('assistant_tool_calls',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('conversation_id', sa.String(length=64), nullable=True),
    sa.Column('message_id', sa.String(length=64), nullable=True),
    sa.Column('opencode_session_id', sa.String(length=128), nullable=True),
    sa.Column('tool_name', sa.String(length=128), nullable=False),
    sa.Column('arguments_json', sa.JSON(), nullable=False),
    sa.Column('result_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['assistant_conversations.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['message_id'], ['assistant_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_assistant_tool_calls_conversation_id'), 'assistant_tool_calls', ['conversation_id'], unique=False)
    op.create_index(op.f('ix_assistant_tool_calls_message_id'), 'assistant_tool_calls', ['message_id'], unique=False)


def downgrade() -> None:
    op.drop_table('assistant_tool_calls')
    op.drop_table('assistant_operations')
    op.drop_table('assistant_message_parts')
    op.drop_table('assistant_context_refs')
    op.drop_table('assistant_messages')
    op.drop_table('assistant_conversations')
