"""Normalize Run waiting, trigger and retry semantics for the final runtime model."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_runtime_final_state"
down_revision = "0015_runtime_orchestrator"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # BLOCKED was historically used both as an executor result signal and a
    # persisted Run state. The final runtime model persists human waiting only
    # as WAITING_INPUT; executor BLOCKED is translated at the coordinator edge.
    op.execute(
        sa.text(
            "UPDATE runs SET status = 'WAITING_INPUT', executor_status = 'WAITING_INPUT' "
            "WHERE status = 'BLOCKED'"
        )
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trigger_reason",
                sa.String(64),
                nullable=False,
                server_default="INITIAL",
            )
        )
        batch_op.add_column(
            sa.Column(
                "retry_index",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("retry_index")
        batch_op.drop_column("trigger_reason")
    # The previous BLOCKED representation is ambiguous: WAITING_INPUT rows
    # cannot be distinguished from rows that used to be BLOCKED, so normalized
    # waiting data is intentionally preserved during downgrade.
