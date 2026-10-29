"""将分裂的 Emoji 配置一次性收敛为与原办公室相同的稳定形象。"""

import sqlalchemy as sa

from alembic import op

revision = "0006_employee_avatars"
down_revision = "0005_dsh_runtime"
branch_labels = None
depends_on = None

# 冻结 1.4.9 首版办公室算法；迁移不依赖未来可能变化的运行时目录/选择器。
ROBOT_IDS = tuple(f"robot-v1-{index}" for index in range(6))
SAVED_IDS = frozenset(
    (
        *ROBOT_IDS,
        *(f"botpixel-v1-{key}" for key in ("beret", "braids", "silver", "headphones", "tie", "hime")),
    )
)


def previous_robot(row) -> str:
    role = f"{row['domain']} {row['description']}"
    # 旧 JS Array.from 按 code point 迭代，charCodeAt(0) 只取首个 UTF-16 unit。
    seed = 0
    for char in row["id"]:
        point = ord(char)
        unit = point if point <= 0xFFFF else 0xD800 + ((point - 0x10000) >> 10)
        seed = (seed * 31 + unit) & 0xFFFFFFFF
    if any(word in role for word in ("自动化", "脚本")):
        group = 0
    elif any(word in role for word in ("系统", "平台", "架构")):
        group = 1
    elif any(word in role for word in ("防线", "运营", "巡检")):
        group = 2
    else:
        group = seed % 3
    return ROBOT_IDS[group * 2 + seed % 2]


def upgrade():
    connection = op.get_bind()
    rows = (
        connection.execute(sa.text("SELECT id, domain, description, avatar FROM t_nexus_employees"))
        .mappings()
        .all()
    )
    for row in rows:
        if row["avatar"] in SAVED_IDS:
            continue
        connection.execute(
            sa.text("UPDATE t_nexus_employees SET avatar=:avatar WHERE id=:id"),
            {"avatar": previous_robot(row), "id": row["id"]},
        )


def downgrade():
    raise RuntimeError("形象回填不猜测原 Emoji；请恢复升级前数据库备份后回滚")
