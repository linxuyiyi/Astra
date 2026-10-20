"""Persist the shared runtime failure contract; normalize retired values once."""

import sqlalchemy as sa

from alembic import op

revision = "0017_runtime_failure_contract"
down_revision = "0016_runtime_final_state"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_failure_type")
        batch.alter_column("failure_type", new_column_name="failure_code", existing_type=sa.String(64))
        batch.add_column(sa.Column("failure_scope", sa.String(32), nullable=True))
        batch.add_column(sa.Column("retryable", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("recovery_action", sa.String(32), nullable=True))
        batch.add_column(sa.Column("raw_error", sa.Text(), nullable=True))
    op.create_index("ix_runs_failure_code", "runs", ["failure_code"])
    op.execute("UPDATE runs SET status = 'COMPLETED' WHERE status = 'PASSED'")
    op.execute("UPDATE runs SET trigger_reason = 'INITIAL' WHERE trigger_reason = 'MANUAL_START'")
    op.execute("UPDATE runs SET trigger_reason = 'USER_CONTINUE' WHERE trigger_reason = 'USER_RERUN'")
    op.execute("UPDATE runs SET trigger_reason = 'WORKER_RECOVERY' WHERE trigger_reason = 'RECOVERY'")
    op.execute(
        "UPDATE runs SET failure_code = 'WORKER_STARTUP_LOST' WHERE failure_code = 'STARTUP_LEASE_LOST'"
    )
    op.execute("UPDATE runs SET failure_code = 'NONZERO_EXIT' WHERE failure_code = 'AUTO_RETRY'")
    op.execute(
        "UPDATE runs SET failure_code = 'SESSION_NOT_FOUND' WHERE failure_code = 'SESSION_UNAVAILABLE'"
    )
    op.execute("UPDATE runs SET failure_code = NULL WHERE status = 'COMPLETED'")
    op.execute(
        "UPDATE runs SET failure_code = 'INVARIANT_VIOLATION' WHERE failure_code = 'MIGRATION_LEASE_CONFLICT'"
    )
    # Freeze policy here so future runtime changes cannot change historical migrations.
    policies = {
        "DISPATCH_FAILED": ("DISPATCH", 1, "RETRY"),
        "WORKER_STARTUP_LOST": ("WORKER", 1, "RETRY"),
        "WORKER_LEASE_LOST": ("WORKER", 1, "CREATE_SESSION"),
        "EXECUTOR_TIMEOUT": ("EXECUTOR", 1, "RESUME_SESSION"),
        "EXECUTOR_EXCEPTION": ("EXECUTOR", 1, "RESUME_SESSION"),
        "NONZERO_EXIT": ("EXECUTOR", 1, "RETRY"),
        "SESSION_NOT_FOUND": ("SESSION", 1, "CREATE_SESSION"),
        "SESSION_ERROR": ("SESSION", 1, "RESUME_SESSION"),
        "ENVIRONMENT_UNAVAILABLE": ("ENVIRONMENT", 1, "WAIT_ENVIRONMENT"),
        "USER_CANCELLED": ("USER", 0, "NONE"),
        "USER_SUSPENDED": ("USER", 0, "NONE"),
        "INVARIANT_VIOLATION": ("SYSTEM", 0, "FAIL"),
    }
    for code, (scope, retryable, action) in policies.items():
        op.execute(
            sa.text(
                f"UPDATE runs SET failure_scope = '{scope}', retryable = {retryable}, "
                f"recovery_action = '{action}' WHERE failure_code = '{code}'"
            )
        )


def downgrade():
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_failure_code")
        for name in ("raw_error", "recovery_action", "retryable", "failure_scope"):
            batch.drop_column(name)
        batch.alter_column("failure_code", new_column_name="failure_type", existing_type=sa.String(64))
    op.create_index("ix_runs_failure_type", "runs", ["failure_type"])
