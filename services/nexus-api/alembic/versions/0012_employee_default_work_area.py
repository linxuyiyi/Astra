"""Add the configurable default digital employee work area."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_employee_default_work_area"
down_revision = "0011_digital_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "digital_employees",
        sa.Column(
            "default_work_area",
            sa.String(32),
            nullable=False,
            server_default="development",
        ),
    )


def downgrade() -> None:
    op.drop_column("digital_employees", "default_work_area")
