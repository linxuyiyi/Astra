"""每次提交 attempt 到 Worker 请求的映射。

body_fetched_at 只有在请求体被取走之后才写入。没有这张映射就不能判断能否重发。
downgrade 删除这一列。
"""

import sqlalchemy as sa

from alembic import op

revision = "0041_submission_attempts"
down_revision = "0040_worker_occupancy"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("submission_attempts", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("tasks", "submission_attempts")
