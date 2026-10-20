"""环境通道标记，以及 Worker 请求体被拉取的时间。

worker_channel 只活在环境行上。body_fetched_at 记录请求体已经离开 Gateway。
downgrade 删除这两列。
"""

import sqlalchemy as sa

from alembic import op

revision = "0042_worker_channel_and_body"
down_revision = "0041_submission_attempts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "opencode_execution_environments",
        sa.Column("worker_channel", sa.String(length=16), nullable=False, server_default="ssh"),
    )
    op.add_column("worker_commands", sa.Column("body_fetched_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("worker_commands", "body_fetched_at")
    op.drop_column("opencode_execution_environments", "worker_channel")
