"""Persist source sync work and resource-level Wiki usage/feedback."""

from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "0019_library_llm_wiki"
down_revision = "0018_merge_runtime_category"
branch_labels = None
depends_on = None


def upgrade():
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
    for table in ("knowledge_usage", "knowledge_feedback"):
        op.add_column(table, sa.Column("resource_id", sa.String(512), nullable=True))
        op.create_index(f"ix_{table}_resource_id", table, ["resource_id"])
    # Existing web feedback remains attached to original knowledge. Wiki feedback
    # has no original FK because generated pages can combine multiple sources.
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys("knowledge_feedback")
    original_fk = next(fk for fk in foreign_keys if fk["constrained_columns"] == ["knowledge_id"])
    original_fk_name = original_fk["name"] or "fk_knowledge_feedback_knowledge_id_knowledge_items"
    with op.batch_alter_table(
        "knowledge_feedback",
        naming_convention={
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        },
    ) as batch:
        batch.drop_constraint(original_fk_name, type_="foreignkey")
        batch.alter_column("knowledge_id", existing_type=sa.String(64), nullable=True)
        batch.create_foreign_key(
            "fk_knowledge_feedback_knowledge_id_knowledge_items",
            "knowledge_items",
            ["knowledge_id"],
            ["id"],
            ondelete="SET NULL",
        )
    bind = op.get_bind()
    # Migrate historical identifiers once; do not pretend Astra IDs are Wiki paths.
    for table in ("knowledge_usage", "knowledge_feedback"):
        metadata = sa.MetaData()
        target = sa.Table(table, metadata, autoload_with=bind)
        bind.execute(
            target.update()
            .where(target.c.knowledge_id.is_not(None))
            .values(resource_id=sa.literal("astra:") + target.c.knowledge_id)
        )
    bind.execute(sa.text("UPDATE knowledge_usage SET action='read' WHERE action='reference'"))
    bind.execute(
        sa.text(
            "UPDATE knowledge_usage SET action='experience_submitted' "
            "WHERE tool_name='library_submit_experience'"
        )
    )
    # A legacy search represented the whole response; expand its hits into resource events.
    usage = sa.Table("knowledge_usage", sa.MetaData(), autoload_with=bind)
    for row in bind.execute(sa.select(usage).where(usage.c.action == "search")).mappings().all():
        result = row["result"] or {}
        for knowledge_id in dict.fromkeys(result.get("knowledge_ids", [])):
            values = dict(row)
            values.pop("id")
            values.update(resource_id=f"astra:{knowledge_id}", action="search_hit", knowledge_id=None)
            bind.execute(usage.insert().values(**values))
        bind.execute(usage.delete().where(usage.c.id == row["id"]))
    for row in bind.execute(sa.text("SELECT id, updated_at FROM knowledge_items WHERE status='published'")):
        bind.execute(
            sa.text(
                "INSERT INTO knowledge_sync "
                "(knowledge_id, revision, status, requested_at) "
                "VALUES (:id, :revision, 'pending', :updated_at)"
            ),
            {"id": row.id, "revision": uuid4().hex, "updated_at": row.updated_at},
        )


def downgrade():
    bind = op.get_bind()
    # Wiki-only feedback cannot exist in the old, non-null original-knowledge schema.
    bind.execute(sa.text("DELETE FROM knowledge_feedback WHERE knowledge_id IS NULL"))
    with op.batch_alter_table("knowledge_feedback") as batch:
        batch.drop_constraint("fk_knowledge_feedback_knowledge_id_knowledge_items", type_="foreignkey")
        batch.alter_column("knowledge_id", existing_type=sa.String(64), nullable=False)
        batch.create_foreign_key(
            "fk_knowledge_feedback_knowledge_id_knowledge_items",
            "knowledge_items",
            ["knowledge_id"],
            ["id"],
            ondelete="CASCADE",
        )
    for table in ("knowledge_usage", "knowledge_feedback"):
        op.drop_index(f"ix_{table}_resource_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("resource_id")
    bind.execute(sa.text("UPDATE knowledge_usage SET action='reference' WHERE action='read'"))
    op.drop_table("knowledge_sync")
