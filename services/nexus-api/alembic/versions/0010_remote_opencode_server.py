"""Add the private OpenCode Server port for Linux remote environments."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_remote_opencode_server"
down_revision = "0009_runtime_event_adapter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.add_column(
            sa.Column("opencode_server_port", sa.Integer(), nullable=False, server_default="14096")
        )


def downgrade() -> None:
    with op.batch_alter_table("opencode_execution_environments") as batch:
        batch.drop_column("opencode_server_port")
