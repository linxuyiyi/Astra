"""Store gateway probes as per-operation runs instead of per-step rows."""

import sqlalchemy as sa

from alembic import op

revision = "0055_gateway_probe_runs"
down_revision = "0054_experience_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "t_nexus_gateway_probe_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "node_id",
            sa.String(64),
            sa.ForeignKey("t_nexus_gateway_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("strategy", sa.String(16), nullable=False, server_default="SCOPED"),
        sa.Column("first_model", sa.String(200), nullable=True),
        sa.Column("runtime_scope", sa.String(200), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="FAILED"),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("usage", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_gateway_probe_runs_node_id", "t_nexus_gateway_probe_runs", ["node_id"]
    )
    op.create_index(
        "ix_gateway_probe_runs_node_time",
        "t_nexus_gateway_probe_runs",
        ["node_id", "started_at"],
    )


def downgrade():
    op.drop_table("t_nexus_gateway_probe_runs")
