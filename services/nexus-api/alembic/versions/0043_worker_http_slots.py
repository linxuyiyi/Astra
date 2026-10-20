"""http 并发上限的认领计数。

每个 Worker、每个 epoch 一行。认领时只有 inflight 小于上限才会加一。
downgrade 删除计数表和指令上的占用标记。
"""

import sqlalchemy as sa

from alembic import op

revision = "0043_worker_http_slots"
down_revision = "0042_worker_channel_and_body"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "worker_http_slots",
        sa.Column("worker_instance_id", sa.String(length=80), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("inflight", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("worker_instance_id", "epoch"),
    )
    op.add_column(
        "worker_commands",
        sa.Column("slot_held", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("worker_commands", "slot_held")
    op.drop_table("worker_http_slots")
