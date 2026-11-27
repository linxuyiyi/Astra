"""为网关模型路由增加视觉能力标记，并新增全平台默认图像理解路由表。

Revision ID: 0002_gateway_vision
Revises: 0001_init
Create Date: 2026-11-27 13:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_gateway_vision"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "t_nexus_gateway_routes",
        sa.Column("supports_vision", sa.Boolean(), server_default="0", nullable=False),
    )
    op.create_table(
        "t_nexus_gateway_vision_input",
        sa.Column("id", sa.String(length=16), nullable=False),
        sa.Column("route_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["route_id"], ["t_nexus_gateway_routes.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("t_nexus_gateway_vision_input")
    op.drop_column("t_nexus_gateway_routes", "supports_vision")
