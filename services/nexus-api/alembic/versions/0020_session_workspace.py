"""Task business states and native Session bindings; retire duplicated execution leases."""

from alembic import op
import sqlalchemy as sa

revision = "0020_session_workspace"
down_revision = "0019_library_llm_wiki"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column(
            "opencode_session_id", new_column_name="current_session_id", existing_type=sa.String(160)
        )
        batch.add_column(sa.Column("workspace", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("runtime_connection", sa.String(32), nullable=False, server_default="UNKNOWN")
        )
        batch.add_column(sa.Column("runtime_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("pending_message_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("last_message_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("turn_started_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("pending_prompt", sa.JSON(), nullable=True))
    op.create_table(
        "runtime_sessions",
        sa.Column("id", sa.String(160), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id"), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=False),
        sa.Column("environment", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_runtime_sessions_task_id", "runtime_sessions", ["task_id"])
    db = op.get_bind()
    metadata = sa.MetaData()
    tasks = sa.Table("tasks", metadata, autoload_with=db)
    runs = sa.Table("runs", metadata, autoload_with=db)
    bindings = sa.Table("runtime_sessions", metadata, autoload_with=db)
    mapping = {
        "READY": "PENDING",
        "ASSIGNED": "PENDING",
        "QUEUED": "PENDING",
        "RUNNING": "RUNNING",
        "WAIT_HUMAN": "WAITING",
        "SUSPENDED": "WAITING",
        "PENDING_CONFIRMATION": "WAITING",
        "COMPLETED": "SUCCEEDED",
        "FAILED": "FAILED",
        "EXECUTION_ERROR": "FAILED",
        "CANCELLED": "CANCELED",
    }
    seen = set()
    for row in db.execute(sa.select(tasks)).mappings():
        snapshot = row["execution_environment_snapshot"] or {}
        workspace = snapshot.get("workspace_root")
        db.execute(
            tasks.update()
            .where(tasks.c.id == row["id"])
            .values(
                status=mapping[row["status"]],
                workspace=workspace,
                active_run_id=None,
            )
        )
        rows = (
            db.execute(sa.select(runs).where(runs.c.task_id == row["id"]).order_by(runs.c.run_number.desc()))
            .mappings()
            .all()
        )
        candidates = [(row["current_session_id"], snapshot, row["created_at"], row["employee_id"])]
        candidates.extend(
            (
                r["opencode_session_id"],
                r["execution_environment_snapshot"] or snapshot,
                r["created_at"],
                r["employee_id"],
            )
            for r in rows
        )
        for sid, environment, created, employee in candidates:
            directory = environment.get("workspace_root")
            if sid and sid not in seen and directory and employee:
                db.execute(
                    bindings.insert().values(
                        id=sid,
                        task_id=row["id"],
                        employee_id=employee,
                        workspace=directory,
                        environment=environment,
                        created_at=created,
                    )
                )
                seen.add(sid)
    op.execute(sa.text("UPDATE digital_employees SET active_run_id = NULL, current_task_count = 0"))
    op.drop_table("run_dispatch_outbox")
    for table in ("tasks", "digital_employees"):
        with op.batch_alter_table(table) as batch:
            # MySQL 8 refuses to drop an index that is still required by a FK.
            # Always remove dependency -> index -> column in that order.
            batch.drop_constraint(f"fk_{table}_active_run_id_runs", type_="foreignkey")
            batch.drop_index(f"ix_{table}_active_run_id")
            batch.drop_column("active_run_id")


def downgrade():
    raise RuntimeError("Session Workspace migration is irreversible; restore a pre-migration backup")
