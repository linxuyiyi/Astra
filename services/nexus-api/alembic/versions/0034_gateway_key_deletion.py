"""Gateway Key 删除保留永久吊销记录。"""

import sqlalchemy as sa

from alembic import op

revision = "0034_gateway_key_deletion"
down_revision = "0033_employee_character"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("gateway_keys", sa.Column("deleted_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("gateway_keys") as batch:
        batch.drop_column("deleted_at")
