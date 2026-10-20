"""Task 的结构化等待原因。只由 Session 投影写入。"""

import sqlalchemy as sa

from alembic import op

revision = "0046_task_waiting_reason"
down_revision = "0045_task_repository_snapshot"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("waiting_reason", sa.String(length=64), nullable=True))


def downgrade():
    op.drop_column("tasks", "waiting_reason")
