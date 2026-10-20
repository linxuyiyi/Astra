"""Independent assistant task reviews; remove template extraction cursor."""

import sqlalchemy as sa

from alembic import op

revision = "0030_assistant_task_reviews"
down_revision = "0029_knowledge_sync_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_reviews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "source_session_id",
            sa.String(160),
            sa.ForeignKey("runtime_sessions.id", ondelete="CASCADE"),
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
    op.create_index("ix_task_reviews_task_id", "task_reviews", ["task_id"])
    with op.batch_alter_table("task_summaries") as batch:
        batch.drop_column("artifacts_processed_generation")


def downgrade():
    with op.batch_alter_table("task_summaries") as batch:
        batch.add_column(
            sa.Column("artifacts_processed_generation", sa.Integer(), nullable=False, server_default="0")
        )
    op.drop_table("task_reviews")
