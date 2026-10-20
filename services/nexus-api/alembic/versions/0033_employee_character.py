"""保存员工形象，一次性保留已有员工在首页的人物。"""

import sqlalchemy as sa

from alembic import op

revision = "0033_employee_character"
down_revision = "0032_gateway_saved_config"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "digital_employees",
        sa.Column("character_id", sa.Integer(), nullable=False, server_default="0"),
    )
    employees = sa.table(
        "digital_employees", sa.column("id", sa.String()), sa.column("character_id", sa.Integer())
    )
    connection = op.get_bind()
    ids = connection.execute(sa.select(employees.c.id)).scalars().all()
    for employee_id in ids:
        value = 7
        for character in employee_id:
            # 对齐旧首页 [...id] + charCodeAt(0) 的 UTF-16 首码元语义。
            code = int.from_bytes(character.encode("utf-16-le", "surrogatepass")[:2], "little")
            value = (value * 31 + code) & 0xFFFFFFFF
        connection.execute(
            employees.update().where(employees.c.id == employee_id).values(character_id=value % 9)
        )


def downgrade():
    op.drop_column("digital_employees", "character_id")
