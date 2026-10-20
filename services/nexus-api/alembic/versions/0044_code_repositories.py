"""仓库和加密 PAT。

环境和 Task 记下 repository_id。PAT 只存密文。
"""

import sqlalchemy as sa

from alembic import op

revision = "0044_code_repositories"
down_revision = "0043_worker_http_slots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "code_repositories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False, server_default="main"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "git_credentials",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("repository_id", sa.String(length=64), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["repository_id"], ["code_repositories.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("repository_id"),
    )
    op.add_column(
        "opencode_execution_environments",
        sa.Column("repository_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "opencode_execution_environments",
        sa.Column("branch", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "opencode_execution_environments",
        sa.Column("skill_refs", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "opencode_execution_environments",
        sa.Column("mcp_refs", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("tasks", sa.Column("repository_id", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_opencode_execution_environments_repository_id",
        "opencode_execution_environments",
        ["repository_id"],
    )
    op.create_index("ix_tasks_repository_id", "tasks", ["repository_id"])


def downgrade():
    op.drop_index("ix_tasks_repository_id", table_name="tasks")
    op.drop_index(
        "ix_opencode_execution_environments_repository_id",
        table_name="opencode_execution_environments",
    )
    op.drop_column("tasks", "repository_id")
    op.drop_column("opencode_execution_environments", "mcp_refs")
    op.drop_column("opencode_execution_environments", "skill_refs")
    op.drop_column("opencode_execution_environments", "branch")
    op.drop_column("opencode_execution_environments", "repository_id")
    op.drop_table("git_credentials")
    op.drop_table("code_repositories")
