"""Task 的关联任务和分配时的 base commit。"""

import sqlalchemy as sa

from alembic import op

revision = "0045_task_repository_snapshot"
down_revision = "0044_code_repositories"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("related_task_id", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("base_commit", sa.String(length=64), nullable=True))
    op.create_index("ix_tasks_related_task_id", "tasks", ["related_task_id"])


def downgrade():
    op.drop_index("ix_tasks_related_task_id", table_name="tasks")
    op.drop_column("tasks", "base_commit")
    op.drop_column("tasks", "related_task_id")
