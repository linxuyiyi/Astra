"""Add an optional per-employee Agent model override."""

import sqlalchemy as sa

from alembic import op

revision = "0005_employee_model"
down_revision = "0004_astra_v04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.add_column(sa.Column("model_name", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.drop_column("model_name")
