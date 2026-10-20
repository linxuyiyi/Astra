"""Task summary and session-anchored experience provenance.

Phase 1B: one TaskSummary per Task, and experience idempotency/verification
anchored on the producing RuntimeSession. Run/Evidence legacy tables are not
touched.
"""

from alembic import op
import sqlalchemy as sa
from uuid import uuid4

revision = "0022_task_summary_experience"
down_revision = "0021_runtime_session_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_summaries",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("primary_session_id", sa.String(160), nullable=True),
        sa.Column("referenced_session_ids", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_md", sa.Text(), nullable=False),
        sa.Column("acceptance_results", sa.JSON(), nullable=False),
        sa.Column("key_changes", sa.JSON(), nullable=False),
        sa.Column("source_status", sa.String(32), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("generated_by", sa.String(120), nullable=False),
        sa.Column("source_ref_snapshot", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["primary_session_id"],
            ["runtime_sessions.id"],
            name="fk_task_summaries_primary_session_id",
            ondelete="SET NULL",
        ),
    )
    op.create_index("ix_task_summaries_task_id", "task_summaries", ["task_id"], unique=True)
    op.create_index("ix_task_summaries_stale", "task_summaries", ["stale"])

    with op.batch_alter_table("knowledge_items") as batch:
        batch.add_column(sa.Column("origin", sa.String(32), nullable=False, server_default="manual"))
        batch.add_column(sa.Column("dedupe_key", sa.String(200), nullable=True))
        batch.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("experience_type", sa.String(64), nullable=True))
        batch.add_column(sa.Column("scope", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("session_status_at_submission", sa.String(32), nullable=True))
        batch.add_column(sa.Column("verification_session_status", sa.String(32), nullable=True))
        batch.add_column(sa.Column("verification_message_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("session_last_message_id_at_verification", sa.String(160), nullable=True))
        batch.add_column(sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("source_ref_snapshot", sa.JSON(), nullable=True))
        batch.create_index("ix_knowledge_items_dedupe_key", ["dedupe_key"])
        batch.create_unique_constraint(
            "uq_knowledge_dedupe", ["dedupe_key", "fingerprint"]
        )

    with op.batch_alter_table("experience_evidence") as batch:
        batch.add_column(sa.Column("source_session_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("message_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("part_id", sa.String(160), nullable=True))
        batch.add_column(sa.Column("file_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("diff_hash", sa.String(64), nullable=True))
        batch.add_column(sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_experience_evidence_source_session_id", ["source_session_id"])
        batch.create_foreign_key(
            "fk_experience_evidence_source_session_id",
            "runtime_sessions",
            ["source_session_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("knowledge_usage") as batch:
        batch.add_column(sa.Column("session_id", sa.String(160), nullable=True))
        batch.create_index("ix_knowledge_usage_session_id", ["session_id"])
        batch.create_foreign_key(
            "fk_knowledge_usage_session_id",
            "runtime_sessions",
            ["session_id"],
            ["id"],
            ondelete="SET NULL",
        )


    db = op.get_bind()
    metadata = sa.MetaData()
    tasks = sa.Table("tasks", metadata, autoload_with=db)
    runs = sa.Table("runs", metadata, autoload_with=db)
    runtime_sessions = sa.Table("runtime_sessions", metadata, autoload_with=db)
    summaries = sa.Table("task_summaries", metadata, autoload_with=db)
    evidence = sa.Table("evidence", metadata, autoload_with=db)
    knowledge_items = sa.Table("knowledge_items", metadata, autoload_with=db)
    experience_evidence = sa.Table("experience_evidence", metadata, autoload_with=db)
    knowledge_usage = sa.Table("knowledge_usage", metadata, autoload_with=db)

    run_rows = db.execute(
        sa.select(runs).order_by(runs.c.task_id, runs.c.run_number)
    ).mappings().all()
    existing_session_ids = set(
        db.execute(sa.select(runtime_sessions.c.id)).scalars().all()
    )
    runs_by_task = {}
    run_to_session = {}
    for row in run_rows:
        runs_by_task.setdefault(row["task_id"], []).append(row)
        if row["opencode_session_id"] in existing_session_ids:
            run_to_session[row["id"]] = row["opencode_session_id"]

    # Preserve old source lineage only when 0020 could create a valid binding.
    for run_id, session_id in run_to_session.items():
        db.execute(
            knowledge_items.update()
            .where(knowledge_items.c.source_run_id == run_id)
            .values(source_session_id=session_id)
        )
        db.execute(
            experience_evidence.update()
            .where(experience_evidence.c.run_id == run_id)
            .values(source_session_id=session_id)
        )
        db.execute(
            knowledge_usage.update()
            .where(knowledge_usage.c.run_id == run_id)
            .values(session_id=session_id)
        )

    evidence_rows = db.execute(sa.select(evidence)).mappings().all()
    evidence_by_task = {}
    for row in evidence_rows:
        evidence_by_task.setdefault(row["task_id"], []).append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "path": row["path"],
                "size": row["size"],
                "content_type": row["content_type"],
                "metadata": row["metadata"],
            }
        )

    for task in db.execute(sa.select(tasks)).mappings():
        task_runs = runs_by_task.get(task["id"], [])
        latest_run = task_runs[-1] if task_runs else None
        legacy_result = task["result"] or (latest_run["result"] if latest_run else None)
        referenced_sessions = [
            row[0]
            for row in db.execute(
                sa.select(runtime_sessions.c.id)
                .where(runtime_sessions.c.task_id == task["id"])
                .order_by(runtime_sessions.c.created_at, runtime_sessions.c.id)
            ).all()
        ]
        primary_session = (
            task["current_session_id"]
            if task["current_session_id"] in referenced_sessions
            else referenced_sessions[-1] if referenced_sessions else None
        )

        terminal = task["status"] in {"SUCCEEDED", "FAILED", "CANCELED"}
        if not legacy_result and not terminal:
            continue

        result = legacy_result if isinstance(legacy_result, dict) else {}
        summary_text = str(
            result.get("summary")
            or task["failure_reason"]
            or task["current_progress"]
            or f"历史任务状态：{task['status']}"
        ).strip()
        acceptance_results = (
            result.get("acceptance_results")
            if isinstance(result.get("acceptance_results"), list)
            else []
        )
        files = result.get("files") if isinstance(result.get("files"), list) else []
        commits = result.get("commits") if isinstance(result.get("commits"), list) else []
        changes = result.get("changes") if isinstance(result.get("changes"), list) else []
        key_changes = {
            "files": files,
            "commits": commits,
            "legacy_changes": changes,
        }
        snapshot = {
            "task_id": task["id"],
            "title": task["title"],
            "task_type": task["task_type"],
            "employee_id": task["employee_id"],
            "source": task["source"],
            "legacy_run_ids": [row["id"] for row in task_runs],
            "legacy_result": legacy_result,
            "legacy_evidence": evidence_by_task.get(task["id"], []),
        }
        content_md = (
            f"# {task['title']}\n\n"
            f"- 任务：{task['id']}\n"
            f"- 历史结果：{task['status']}\n"
            f"- 完成 Session：{primary_session or '-'}\n\n"
            f"## 结果摘要\n\n{summary_text}"
        )
        generated_at = (
            task["finished_at"]
            or task["updated_at"]
            or task["created_at"]
        )
        db.execute(
            summaries.insert().values(
                id=f"TS-{uuid4().hex[:12]}",
                task_id=task["id"],
                primary_session_id=primary_session,
                referenced_session_ids=referenced_sessions,
                summary=summary_text,
                content_md=content_md,
                acceptance_results=acceptance_results,
                key_changes=key_changes,
                source_status=task["status"],
                generation=1,
                stale=False,
                generated_by="migration:legacy-result",
                source_ref_snapshot=snapshot,
                generated_at=generated_at,
                updated_at=generated_at,
            )
        )


def downgrade():
    with op.batch_alter_table("knowledge_usage") as batch:
        batch.drop_constraint("fk_knowledge_usage_session_id", type_="foreignkey")
        batch.drop_index("ix_knowledge_usage_session_id")
        batch.drop_column("session_id")

    with op.batch_alter_table("experience_evidence") as batch:
        batch.drop_constraint("fk_experience_evidence_source_session_id", type_="foreignkey")
        batch.drop_index("ix_experience_evidence_source_session_id")
        for column in ("captured_at", "diff_hash", "file_path", "part_id", "message_id", "source_session_id"):
            batch.drop_column(column)

    with op.batch_alter_table("knowledge_items") as batch:
        batch.drop_constraint("uq_knowledge_dedupe", type_="unique")
        batch.drop_index("ix_knowledge_items_dedupe_key")
        for column in (
            "source_ref_snapshot",
            "verified_at",
            "session_last_message_id_at_verification",
            "verification_message_id",
            "verification_session_status",
            "session_status_at_submission",
            "scope",
            "experience_type",
            "fingerprint",
            "dedupe_key",
            "origin",
        ):
            batch.drop_column(column)

    op.drop_index("ix_task_summaries_stale", table_name="task_summaries")
    op.drop_index("ix_task_summaries_task_id", table_name="task_summaries")
    op.drop_table("task_summaries")