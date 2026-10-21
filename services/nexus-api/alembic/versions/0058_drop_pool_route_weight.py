"""Drop the unused pool/route weight columns; node weight remains the only weight."""

from alembic import op

revision = "0058_drop_pool_route_weight"
down_revision = "0057_gateway_default_pool_and_node_weight"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("t_nexus_gateway_pools") as batch:
        batch.drop_column("weight")
    with op.batch_alter_table("t_nexus_gateway_routes") as batch:
        batch.drop_column("weight")


def downgrade():
    import sqlalchemy as sa

    with op.batch_alter_table("t_nexus_gateway_pools") as batch:
        batch.add_column(sa.Column("weight", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("t_nexus_gateway_routes") as batch:
        batch.add_column(sa.Column("weight", sa.Integer(), nullable=False, server_default="1"))
