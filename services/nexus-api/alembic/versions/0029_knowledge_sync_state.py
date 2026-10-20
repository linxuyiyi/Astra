"""Move LLM Wiki sync state onto knowledge and drop the filesystem queue.

Sync state is now an attribute of each knowledge record (``sync_status`` et al.)
rather than a separate durable queue, because Astra writes through the llm_wiki
source REST API instead of the project directory. Historical pending rows become
``pending`` so the first explicit resync re-pushes them.
"""

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0029_knowledge_sync_state"
down_revision = "0028_usage_breakdowns_and_task_completion"
branch_labels = None
depends_on = None

_SOURCE_PREFIX = "raw/sources/astra/knowledge/"


def upgrade():
    with op.batch_alter_table("knowledge_items") as batch:
        batch.add_column(
            sa.Column(
                "sync_status",
                sa.String(16),
                nullable=False,
                server_default="not_synced",
            )
        )
        batch.add_column(sa.Column("sync_error", sa.Text(), nullable=True))
        batch.add_column(sa.Column("synced_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("source_path", sa.String(512), nullable=True))
        batch.create_index("ix_knowledge_items_sync_status", ["sync_status"])

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id FROM knowledge_items WHERE status = 'published'")
    ).all()
    update = sa.text(
        "UPDATE knowledge_items SET sync_status = 'pending', source_path = :path WHERE id = :id"
    )
    for row in rows:
        connection.execute(update, {"id": row.id, "path": f"{_SOURCE_PREFIX}{row.id}.md"})

    op.drop_table("knowledge_sync")


def downgrade():
    op.create_table(
        "knowledge_sync",
        sa.Column("knowledge_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("synced_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_knowledge_sync_status", "knowledge_sync", ["status"])

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, updated_at FROM knowledge_items WHERE status = 'published'")
    ).all()
    insert = sa.text(
        "INSERT INTO knowledge_sync (knowledge_id, revision, status, requested_at) "
        "VALUES (:id, :revision, 'pending', :requested_at)"
    )
    for row in rows:
        connection.execute(
            insert,
            {"id": row.id, "revision": uuid.uuid4().hex, "requested_at": row.updated_at},
        )

    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_index("ix_knowledge_items_sync_status")
        batch.drop_column("source_path")
        batch.drop_column("synced_at")
        batch.drop_column("sync_error")
        batch.drop_column("sync_status")