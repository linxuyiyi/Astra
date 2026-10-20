"""Add target_model to gateway_routes and actual_model to gateway_requests.

Supports transparent model A -> B routing and per-request model auditing.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049_gateway_target_model"
down_revision = "0048_worker_maintenance"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "gateway_routes",
        sa.Column("target_model", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "gateway_requests",
        sa.Column("actual_model", sa.String(length=200), nullable=True),
    )


def downgrade():
    op.drop_column("gateway_requests", "actual_model")
    op.drop_column("gateway_routes", "target_model")
