"""Rename the task-platform employee table to agent_workers.

The task platform has no digital employees. Each row in this table is an Agent
Worker; the Nexus digital employee is referenced by `digital_employee_id`.
SQLite updates foreign-key references when the table is renamed.
"""

from alembic import op

revision = "0051_rename_workers_table"
down_revision = "0050_worker_digital_employee"
branch_labels = None
depends_on = None


def upgrade():
    op.rename_table("digital_employees", "agent_workers")
    # SQLite keeps index names after a table rename; rename the two worker
    # indexes so the schema matches the model and future autogenerate.
    op.drop_index("ix_digital_employees_status", table_name="agent_workers")
    op.create_index("ix_agent_workers_status", "agent_workers", ["status"])
    op.drop_index("ix_digital_employees_digital_employee_id", table_name="agent_workers")
    op.create_index(
        "ix_agent_workers_digital_employee_id", "agent_workers", ["digital_employee_id"]
    )


def downgrade():
    op.drop_index("ix_agent_workers_digital_employee_id", table_name="agent_workers")
    op.create_index(
        "ix_digital_employees_digital_employee_id", "agent_workers", ["digital_employee_id"]
    )
    op.drop_index("ix_agent_workers_status", table_name="agent_workers")
    op.create_index("ix_digital_employees_status", "agent_workers", ["status"])
    op.rename_table("agent_workers", "digital_employees")
