"""Persist task and question prompts on task categories."""

import sqlalchemy as sa

from alembic import op

revision = "0016_category_prompts"
down_revision = "0015_runtime_orchestrator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_categories", sa.Column("task_prompt", sa.Text(), nullable=True))
    op.add_column("task_categories", sa.Column("question_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_categories", "question_prompt")
    op.drop_column("task_categories", "task_prompt")
