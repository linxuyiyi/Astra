"""Replace SSH agent/key configuration with per-environment passwords."""

import sqlalchemy as sa

from alembic import op

revision = "0006_ssh_password"
down_revision = "0005_employee_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.add_column(sa.Column("ssh_password", sa.Text(), nullable=True))
        batch.drop_column("ssh_key_path")
        batch.drop_column("auth_method")


def downgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.add_column(
            sa.Column(
                "auth_method",
                sa.String(32),
                nullable=False,
                server_default="SSH_AGENT",
            )
        )
        batch.add_column(sa.Column("ssh_key_path", sa.Text(), nullable=True))
        batch.drop_column("ssh_password")
