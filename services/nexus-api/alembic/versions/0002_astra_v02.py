"""Astra V0.2 execution environments and usage telemetry."""

import sqlalchemy as sa

from alembic import context, op

revision = "0002_astra_v02"
down_revision = "0001_astra_v01"
branch_labels = None
depends_on = None


def json_default(value):
    if op.get_context().dialect.name == "mysql":
        if isinstance(value, list):
            return sa.func.json_array(*value)
        return sa.func.json_object()
    return value


def upgrade() -> None:
    op.add_column("digital_employees", sa.Column("skills", sa.JSON(), nullable=True))
    op.add_column(
        "digital_employees",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "digital_employees",
        sa.Column("auto_claim", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "digital_employees",
        sa.Column("auto_execute", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "digital_employees",
        sa.Column("auto_repair", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "digital_employees",
        sa.Column("max_auto_retries", sa.Integer(), nullable=False, server_default="3"),
    )
    connection = op.get_bind()
    employees_for_defaults = sa.table(
        "digital_employees", sa.column("skills", sa.JSON())
    )
    connection.execute(
        employees_for_defaults.update()
        .where(employees_for_defaults.c.skills.is_(None))
        .values(skills=json_default([]))
    )
    with op.batch_alter_table("digital_employees") as batch:
        batch.alter_column("skills", existing_type=sa.JSON(), nullable=False)

    op.create_table(
        "opencode_execution_environments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "employee_id",
            sa.String(64),
            sa.ForeignKey("digital_employees.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("host", sa.String(255)),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(120)),
        sa.Column("auth_method", sa.String(32), nullable=False),
        sa.Column("ssh_key_path", sa.Text()),
        sa.Column("opencode_path", sa.Text(), nullable=False),
        sa.Column("workspace_root", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("status_detail", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_opencode_execution_environments_employee_id",
        "opencode_execution_environments",
        ["employee_id"],
        unique=True,
    )
    op.create_index(
        "ix_opencode_execution_environments_mode",
        "opencode_execution_environments",
        ["mode"],
    )
    op.create_index(
        "ix_opencode_execution_environments_status",
        "opencode_execution_environments",
        ["status"],
    )

    employees = sa.table("digital_employees", sa.column("id", sa.String()))
    environments = sa.table(
        "opencode_execution_environments",
        sa.column("employee_id", sa.String()),
        sa.column("mode", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("port", sa.Integer()),
        sa.column("auth_method", sa.String()),
        sa.column("opencode_path", sa.Text()),
        sa.column("workspace_root", sa.Text()),
        sa.column("status", sa.String()),
        sa.column("status_detail", sa.JSON()),
        sa.column("labels", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    now = sa.func.now()
    if not context.is_offline_mode():
        for employee_id in connection.execute(sa.select(employees.c.id)).scalars():
            connection.execute(
                environments.insert().values(
                    employee_id=employee_id,
                    mode="LOCAL",
                    name="本地 OpenCode",
                    description="由 V0.1 升级生成的默认本地执行环境",
                    port=22,
                    auth_method="SSH_AGENT",
                    opencode_path="opencode",
                    workspace_root="./workspaces",
                    status="UNKNOWN",
                    status_detail={},
                    labels=[],
                    created_at=now,
                    updated_at=now,
                )
            )

    op.add_column("tasks", sa.Column("execution_environment_snapshot", sa.JSON()))
    op.add_column("runs", sa.Column("execution_environment_snapshot", sa.JSON(), nullable=True))
    runs_for_defaults = sa.table(
        "runs", sa.column("execution_environment_snapshot", sa.JSON())
    )
    connection.execute(
        runs_for_defaults.update()
        .where(runs_for_defaults.c.execution_environment_snapshot.is_(None))
        .values(execution_environment_snapshot=json_default({}))
    )
    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "execution_environment_snapshot", existing_type=sa.JSON(), nullable=False
        )
    op.add_column("runs", sa.Column("model_name", sa.String(160)))
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "llm_call_count",
        "tool_call_count",
    ):
        op.add_column("runs", sa.Column(name, sa.Integer()))

    op.create_table(
        "tool_usages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("tool_name", sa.String(160), nullable=False),
        sa.Column("call_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.UniqueConstraint("run_id", "tool_name", name="uq_run_tool_name"),
    )
    op.create_index("ix_tool_usages_run_id", "tool_usages", ["run_id"])
    op.create_index("ix_tool_usages_tool_name", "tool_usages", ["tool_name"])


def downgrade() -> None:
    op.drop_table("tool_usages")
    for name in (
        "tool_call_count",
        "llm_call_count",
        "total_tokens",
        "cache_write_tokens",
        "cache_read_tokens",
        "output_tokens",
        "input_tokens",
        "model_name",
        "execution_environment_snapshot",
    ):
        op.drop_column("runs", name)
    op.drop_column("tasks", "execution_environment_snapshot")
    op.drop_table("opencode_execution_environments")
    for name in (
        "max_auto_retries",
        "auto_repair",
        "auto_execute",
        "auto_claim",
        "enabled",
        "skills",
    ):
        op.drop_column("digital_employees", name)
