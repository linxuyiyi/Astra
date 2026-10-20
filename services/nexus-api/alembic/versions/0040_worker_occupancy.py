"""任务占用确认与 pending_attempt。

人工确认执行停止后才放开已取消任务对员工的占用。确认绑定当时的 Session 和 attempt。
downgrade 删除这些列。
"""

import sqlalchemy as sa

from alembic import op

revision = "0040_worker_occupancy"
down_revision = "0039_worker_gateway"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "tasks",
        sa.Column("pending_attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("tasks", sa.Column("execution_stop_confirmed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "tasks", sa.Column("execution_stop_session_id", sa.String(length=160), nullable=True)
    )
    op.add_column("tasks", sa.Column("execution_stop_attempt", sa.Integer(), nullable=True))
    op.add_column(
        "tasks", sa.Column("execution_stop_operator", sa.String(length=120), nullable=True)
    )
    op.add_column("tasks", sa.Column("execution_stop_reason", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("tasks", "execution_stop_reason")
    op.drop_column("tasks", "execution_stop_operator")
    op.drop_column("tasks", "execution_stop_attempt")
    op.drop_column("tasks", "execution_stop_session_id")
    op.drop_column("tasks", "execution_stop_confirmed_at")
    op.drop_column("tasks", "pending_attempt")
