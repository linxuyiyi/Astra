"""为执行环境和 Session 增加显式引擎与最小原生绑定。"""

import sqlalchemy as sa

from alembic import op

revision = "0005_dsh_runtime"
down_revision = "0004_remove_local_auth"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "t_nexus_opencode_execution_environments",
        sa.Column("runtime_engine", sa.String(16), nullable=False, server_default="opencode"),
    )
    op.add_column(
        "t_nexus_runtime_sessions",
        sa.Column("runtime_engine", sa.String(16), nullable=False, server_default="opencode"),
    )
    op.add_column("t_nexus_runtime_sessions", sa.Column("native_session_id", sa.String(160)))
    op.add_column("t_nexus_runtime_sessions", sa.Column("engine_metadata", sa.JSON()))
    op.add_column(
        "t_nexus_runtime_sessions",
        sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE t_nexus_runtime_sessions SET native_session_id=id")


def downgrade():
    for name in ("fencing_token", "engine_metadata", "native_session_id", "runtime_engine"):
        op.drop_column("t_nexus_runtime_sessions", name)
    op.drop_column("t_nexus_opencode_execution_environments", "runtime_engine")
