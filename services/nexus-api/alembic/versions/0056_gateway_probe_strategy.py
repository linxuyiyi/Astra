"""Add the per-node probe strategy used by verification rounds."""

import sqlalchemy as sa

from alembic import op

revision = "0056_gateway_probe_strategy"
down_revision = "0055_gateway_probe_runs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "t_nexus_gateway_nodes",
        sa.Column(
            "probe_strategy", sa.String(16), nullable=False, server_default="SSE"
        ),
    )


def downgrade():
    op.drop_column("t_nexus_gateway_nodes", "probe_strategy")
