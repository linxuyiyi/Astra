"""回填任务经验的空间与来源，以及网关密钥的最近使用时间。

历史候选经验写成了界面不认识的 source_type，且 space_id 为空，所以图书馆显示
「未归类 / 人工创建」，五个空间计数都是 0。网关调用日志有 key_id，但密钥行的
last_used_at 从未写入。
"""

import sqlalchemy as sa

from alembic import op

revision = "0036_experience_space_and_key_last_used"
down_revision = "0035_task_responsibility"
branch_labels = None
depends_on = None

# 与 bootstrap.CATEGORY_KNOWLEDGE_SPACE 保持一致。迁移不导入应用代码。
_CATEGORY_SPACE = (
    ("testing-platform", "SPACE_TESTING"),
    ("automation-engineering", "SPACE_DEVELOPMENT"),
    ("quality-defense", "SPACE_TESTING"),
)
_DEFAULT_SPACE = "SPACE_PUBLIC"


def upgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE gateway_keys
            SET last_used_at = (
                SELECT MAX(gateway_requests.started_at)
                FROM gateway_requests
                WHERE gateway_requests.key_id = gateway_keys.id
            )
            WHERE last_used_at IS NULL
              AND EXISTS (
                SELECT 1 FROM gateway_requests
                WHERE gateway_requests.key_id = gateway_keys.id
              )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            UPDATE knowledge_items
            SET source_type = 'task'
            WHERE type = 'experience'
              AND source_task_id IS NOT NULL
              AND (source_type IS NULL OR source_type = 'agent_experience')
            """
        )
    )
    for category_id, space_id in _CATEGORY_SPACE:
        conn.execute(
            sa.text(
                """
                UPDATE knowledge_items
                SET space_id = :space_id
                WHERE type = 'experience'
                  AND space_id IS NULL
                  AND source_task_id IS NOT NULL
                  AND source_task_id IN (
                    SELECT id FROM tasks WHERE category_id = :category_id
                  )
                  AND EXISTS (SELECT 1 FROM knowledge_spaces WHERE id = :space_id)
                """
            ),
            {"space_id": space_id, "category_id": category_id},
        )
    conn.execute(
        sa.text(
            """
            UPDATE knowledge_items
            SET space_id = :space_id
            WHERE type = 'experience'
              AND space_id IS NULL
              AND source_type = 'task'
              AND source_task_id IS NOT NULL
              AND EXISTS (SELECT 1 FROM knowledge_spaces WHERE id = :space_id)
            """
        ),
        {"space_id": _DEFAULT_SPACE},
    )


def downgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE knowledge_items
            SET source_type = 'agent_experience'
            WHERE type = 'experience'
              AND source_type = 'task'
              AND origin = 'assistant'
            """
        )
    )
