"""Persist task Git baseline and idempotent terminal artifact progress."""

from alembic import op
import sqlalchemy as sa

revision = "0026_runtime_recovery_facts"
down_revision = "0025_drop_task_result"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("digital_employees") as batch:
        batch.drop_column("max_auto_retries")
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.add_column(sa.Column("workspace_base_commit", sa.String(64), nullable=True))
    with op.batch_alter_table("task_summaries") as batch:
        batch.add_column(
            sa.Column(
                "artifacts_processed_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )

    # Existing summaries were produced before this durable cursor existed.
    # Treat their already-finished post-processing as the baseline rather than
    # re-extracting historical experiences on the first v0.6 startup.
    db = op.get_bind()
    db.execute(
        sa.text(
            "UPDATE task_summaries "
            "SET artifacts_processed_generation = generation"
        )
    )


def downgrade():
    with op.batch_alter_table("digital_employees") as batch:
        batch.add_column(sa.Column("max_auto_retries", sa.Integer(), nullable=False, server_default="3"))
    with op.batch_alter_table("task_summaries") as batch:
        batch.drop_column("artifacts_processed_generation")
    with op.batch_alter_table("runtime_sessions") as batch:
        batch.drop_column("workspace_base_commit")
