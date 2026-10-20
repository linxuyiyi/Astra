"""Add the NEXUS digital library knowledge and audit model."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_digital_library"
down_revision = "0010_remote_opencode_server"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_spaces",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parent_id", sa.String(64), sa.ForeignKey("knowledge_spaces.id"), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("access_scope", sa.String(160), nullable=False, server_default="public"),
        sa.Column("created_by", sa.String(120), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_knowledge_spaces_parent_id", "knowledge_spaces", ["parent_id"])
    op.create_index("ix_knowledge_spaces_sort_order", "knowledge_spaces", ["sort_order"])
    op.create_index("ix_knowledge_spaces_status", "knowledge_spaces", ["status"])
    op.create_index("ix_knowledge_spaces_access_scope", "knowledge_spaces", ["access_scope"])

    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("space_id", sa.String(64), sa.ForeignKey("knowledge_spaces.id"), nullable=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("type", sa.String(32), nullable=False, server_default="document"),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="unverified"),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=True),
        sa.Column("source_ref", sa.String(240), nullable=True),
        sa.Column(
            "source_employee_id",
            sa.String(64),
            sa.ForeignKey("digital_employees.id"),
            nullable=True,
        ),
        sa.Column("source_task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("source_run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("experience_data", sa.JSON(), nullable=False),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(120), nullable=False),
        sa.Column("updated_by", sa.String(120), nullable=False),
        sa.Column("published_by", sa.String(120), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("reference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("helpful_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for name in (
        "space_id",
        "title",
        "type",
        "status",
        "verification_status",
        "source_employee_id",
        "source_task_id",
        "source_run_id",
    ):
        op.create_index(f"ix_knowledge_items_{name}", "knowledge_items", [name])
    op.create_index("ix_knowledge_items_browse", "knowledge_items", ["status", "space_id", "updated_at"])
    op.create_index(
        "ix_knowledge_items_experience",
        "knowledge_items",
        ["type", "status", "published_at"],
    )

    op.create_table(
        "knowledge_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(160), nullable=True),
        sa.Column("source_name", sa.String(240), nullable=True),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("version", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_knowledge_sources_knowledge_id", "knowledge_sources", ["knowledge_id"])

    op.create_table(
        "knowledge_relations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(32), nullable=False, server_default="related"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "source_knowledge_id",
            "target_knowledge_id",
            "relation_type",
            name="uq_knowledge_relation",
        ),
    )
    op.create_index(
        "ix_knowledge_relations_source_knowledge_id",
        "knowledge_relations",
        ["source_knowledge_id"],
    )
    op.create_index(
        "ix_knowledge_relations_target_knowledge_id",
        "knowledge_relations",
        ["target_knowledge_id"],
    )

    op.create_table(
        "knowledge_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id"), nullable=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for name in ("knowledge_id", "employee_id", "task_id", "run_id", "tool_name", "action", "created_at"):
        op.create_index(f"ix_knowledge_usage_{name}", "knowledge_usage", [name])
    op.create_index("ix_knowledge_usage_summary", "knowledge_usage", ["action", "created_at"])

    op.create_table(
        "knowledge_feedback",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_key", sa.String(200), nullable=False),
        sa.Column("user_id", sa.String(120), nullable=True),
        sa.Column("employee_id", sa.String(64), sa.ForeignKey("digital_employees.id"), nullable=True),
        sa.Column("feedback_type", sa.String(32), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("knowledge_id", "actor_key", "feedback_type", name="uq_knowledge_feedback_actor"),
    )
    for name in ("knowledge_id", "user_id", "employee_id", "feedback_type", "task_id", "status"):
        op.create_index(f"ix_knowledge_feedback_{name}", "knowledge_feedback", [name])

    op.create_table(
        "knowledge_favorites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(120), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("knowledge_id", "user_id", name="uq_knowledge_favorite_user"),
    )
    op.create_index("ix_knowledge_favorites_knowledge_id", "knowledge_favorites", ["knowledge_id"])
    op.create_index("ix_knowledge_favorites_user_id", "knowledge_favorites", ["user_id"])

    op.create_table(
        "experience_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("evidence_type", sa.String(64), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("result", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for name in ("knowledge_id", "task_id", "run_id"):
        op.create_index(f"ix_experience_evidence_{name}", "experience_evidence", [name])

    op.create_table(
        "knowledge_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_id",
            sa.String(64),
            sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("actor_id", sa.String(120), nullable=False),
        sa.Column("actor_role", sa.String(32), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for name in ("knowledge_id", "action", "actor_id", "created_at"):
        op.create_index(f"ix_knowledge_audit_{name}", "knowledge_audit", [name])


def downgrade() -> None:
    op.drop_table("knowledge_audit")
    op.drop_table("experience_evidence")
    op.drop_table("knowledge_favorites")
    op.drop_table("knowledge_feedback")
    op.drop_table("knowledge_usage")
    op.drop_table("knowledge_relations")
    op.drop_table("knowledge_sources")
    op.drop_table("knowledge_items")
    op.drop_table("knowledge_spaces")
