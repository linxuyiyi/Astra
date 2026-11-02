"""分离期望配置与节点实时观测，移除历史通道与中断核对列。"""

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "0007_worker_readiness_convergence"
down_revision = "0006_employee_avatars"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "t_nexus_worker_instances",
        sa.Column("target_digest", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "t_nexus_worker_instances",
        sa.Column("observed_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "t_nexus_worker_instances",
        sa.Column(
            "observed_at",
            sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=True,
        ),
    )

    op.execute(
        "UPDATE t_nexus_worker_maintenance_operations "
        "SET state = 'FAILED', stage = 'failed', active_environment_id = NULL "
        "WHERE state = 'INTERRUPTED'"
    )
    op.execute(
        "UPDATE t_nexus_worker_maintenance_operations "
        "SET state = 'RUNNING' "
        "WHERE state IN ('PREFLIGHT', 'VERIFYING')"
    )

    with op.batch_alter_table("t_nexus_opencode_execution_environments") as batch_op:
        batch_op.drop_column("worker_channel")

    with op.batch_alter_table("t_nexus_worker_maintenance_operations") as batch_op:
        batch_op.drop_column("danger_dispatched")
        batch_op.drop_column("reconcile_required")
        batch_op.drop_column("reconcile_only")


def downgrade():
    with op.batch_alter_table("t_nexus_worker_maintenance_operations") as batch_op:
        batch_op.add_column(sa.Column("reconcile_only", sa.Boolean(), server_default="0", nullable=False))
        batch_op.add_column(
            sa.Column("reconcile_required", sa.Boolean(), server_default="0", nullable=False)
        )
        batch_op.add_column(
            sa.Column("danger_dispatched", sa.Boolean(), server_default="0", nullable=False)
        )

    with op.batch_alter_table("t_nexus_opencode_execution_environments") as batch_op:
        batch_op.add_column(
            sa.Column("worker_channel", sa.String(length=16), server_default="ssh", nullable=False)
        )

    with op.batch_alter_table("t_nexus_worker_instances") as batch_op:
        batch_op.drop_column("observed_at")
        batch_op.drop_column("observed_snapshot")
        batch_op.drop_column("target_digest")
