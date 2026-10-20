"""Associate Agent Workers with a Nexus digital employee.

A digital employee can own many Agent Workers, so the reference is not unique.
The Nexus employee catalog lives in another service; this column is an external
reference without a database foreign key.
"""

import sqlalchemy as sa

from alembic import op

revision = "0050_worker_digital_employee"
down_revision = "0049_gateway_target_model"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "digital_employees",
        sa.Column("digital_employee_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_digital_employees_digital_employee_id",
        "digital_employees",
        ["digital_employee_id"],
    )


def downgrade():
    op.drop_index("ix_digital_employees_digital_employee_id", table_name="digital_employees")
    op.drop_column("digital_employees", "digital_employee_id")
