"""Astra V0.4 continuity, recovery, confirmation and activity tracking."""

import sqlalchemy as sa

from alembic import op

revision = "0004_astra_v04"
down_revision = "0003_astra_v03"
branch_labels = None
depends_on = None


def json_default(value):
    if op.get_context().dialect.name == "mysql":
        return sa.func.json_array(*value)
    return value


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("completion_policy", sa.String(32), nullable=False, server_default="AUTO"))
        batch.add_column(sa.Column("suspension", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("execution_error", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("review_history", sa.JSON(), nullable=True))
        batch.add_column(
            sa.Column("inactivity_alert", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("inactivity_alert_since", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_tasks_inactivity_alert", "tasks", ["inactivity_alert"])
    op.create_index("ix_tasks_last_activity_at", "tasks", ["last_activity_at"])

    tasks = sa.table(
        "tasks",
        sa.column("priority", sa.String()),
        sa.column("status", sa.String()),
        sa.column("completion_policy", sa.String()),
        sa.column("review_history", sa.JSON()),
        sa.column("last_activity_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    connection.execute(
        tasks.update()
        .where(tasks.c.review_history.is_(None))
        .values(review_history=json_default([]))
    )
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("review_history", existing_type=sa.JSON(), nullable=False)
    connection.execute(
        tasks.update()
        .where(tasks.c.priority.in_(["VERY_HIGH", "HIGH"]))
        .values(completion_policy="MANUAL_CONFIRM")
    )
    connection.execute(tasks.update().where(tasks.c.status == "PAUSED").values(status="SUSPENDED"))
    connection.execute(tasks.update().values(last_activity_at=tasks.c.updated_at))


def downgrade() -> None:
    tasks = sa.table("tasks", sa.column("status", sa.String()))
    connection = op.get_bind()
    for current, legacy in {
        "SUSPENDED": "PAUSED",
        "PENDING_CONFIRMATION": "COMPLETED",
        "EXECUTION_ERROR": "WAIT_HUMAN",
    }.items():
        connection.execute(tasks.update().where(tasks.c.status == current).values(status=legacy))

    op.drop_index("ix_tasks_last_activity_at", table_name="tasks")
    op.drop_index("ix_tasks_inactivity_alert", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("confirmed_at")
        batch.drop_column("submitted_at")
        batch.drop_column("last_activity_at")
        batch.drop_column("inactivity_alert_since")
        batch.drop_column("inactivity_alert")
        batch.drop_column("review_history")
        batch.drop_column("execution_error")
        batch.drop_column("suspension")
        batch.drop_column("completion_policy")
