"""Drop deprecated Task.result column.

TaskSummary is the durable result record on the Session fact chain; the old
Task.result JSON cache is no longer produced or read.
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_drop_task_result"
down_revision = "0024_drop_legacy_run_models"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("result")


def downgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("result", sa.JSON(), nullable=True))