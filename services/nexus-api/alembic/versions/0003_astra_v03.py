"""Astra V0.3 task management boundary and configurable work scope."""

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import context, op

revision = "0003_astra_v03"
down_revision = "0002_astra_v02"
branch_labels = None
depends_on = None


def json_default(value):
    if op.get_context().dialect.name == "mysql":
        if isinstance(value, list):
            return sa.func.json_array(*value)
        return sa.func.json_object()
    return value


DEFAULT_CATEGORIES = (
    ("testing-platform", "测试平台", "测试平台相关问题与需求", 10),
    ("automation-engineering", "自动化工程", "自动化工程建设与维护", 20),
    ("quality-defense", "防线运营", "质量防线运行与治理", 30),
)


def upgrade() -> None:
    op.create_table(
        "task_categories",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_categories_enabled", "task_categories", ["enabled"])
    op.create_index("ix_task_categories_sort_order", "task_categories", ["sort_order"])

    with op.batch_alter_table("digital_employees") as batch:
        batch.add_column(
            sa.Column(
                "task_types",
                sa.JSON(),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "category_ids",
                sa.JSON(),
                nullable=True,
            )
        )

    connection = op.get_bind()
    employees_for_defaults = sa.table(
        "digital_employees",
        sa.column("task_types", sa.JSON()),
        sa.column("category_ids", sa.JSON()),
    )
    connection.execute(
        employees_for_defaults.update().values(
            task_types=json_default(["ISSUE", "REQUIREMENT"]),
            category_ids=json_default(
                ["testing-platform", "automation-engineering", "quality-defense"]
            ),
        )
    )
    with op.batch_alter_table("digital_employees") as batch:
        batch.alter_column("task_types", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("category_ids", existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("category_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("acceptance_criteria", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("attachments", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("current_progress", sa.Text(), nullable=True))
        batch.add_column(sa.Column("human_intervention", sa.JSON(), nullable=True))
        batch.alter_column("priority", type_=sa.String(16), existing_type=sa.String(8))

    op.create_index("ix_tasks_category_id", "tasks", ["category_id"])

    now = datetime.now(UTC)
    categories = sa.table(
        "task_categories",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("enabled", sa.Boolean()),
        sa.column("sort_order", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection.execute(
        categories.insert(),
        [
            {
                "id": category_id,
                "name": name,
                "description": description,
                "enabled": True,
                "sort_order": sort_order,
                "created_at": now,
                "updated_at": now,
            }
            for category_id, name, description, sort_order in DEFAULT_CATEGORIES
        ],
    )

    tasks = sa.table(
        "tasks",
        sa.column("id", sa.String()),
        sa.column("task_type", sa.String()),
        sa.column("definition_of_done", sa.JSON()),
        sa.column("acceptance_criteria", sa.JSON()),
        sa.column("attachments", sa.JSON()),
        sa.column("category_id", sa.String()),
        sa.column("priority", sa.String()),
        sa.column("status", sa.String()),
        sa.column("current_phase", sa.String()),
        sa.column("current_progress", sa.Text()),
        sa.column("result", sa.JSON()),
    )
    connection.execute(
        tasks.update().values(
            acceptance_criteria=json_default([]),
            attachments=json_default([]),
            current_progress="等待领取",
        )
    )
    if not context.is_offline_mode():
        for row in connection.execute(
            sa.select(
                tasks.c.id,
                tasks.c.task_type,
                tasks.c.definition_of_done,
                tasks.c.current_phase,
                tasks.c.result,
            )
        ).mappings():
            criteria = [
                {"id": f"AC-{index:02d}", "content": str(content)}
                for index, content in enumerate(row["definition_of_done"] or [], start=1)
            ]
            legacy_type = str(row["task_type"] or "").lower()
            task_type = (
                "ISSUE"
                if any(word in legacy_type for word in ("repair", "fix", "bug"))
                else "REQUIREMENT"
            )
            progress = (
                str(row["current_phase"] or "").replace("_", " ").strip() or "等待领取"
            )
            legacy_result = row["result"]
            result = None
            if legacy_result is not None:
                legacy_payload = (
                    legacy_result if isinstance(legacy_result, dict) else {"value": legacy_result}
                )
                result = {
                    "summary": str(legacy_payload.get("summary") or "历史任务结果已迁移"),
                    "changes": [],
                    "files": [],
                    "commits": [],
                    "logs": [],
                    "attachments": [],
                    "other": {"v02_result": legacy_payload},
                    "acceptance_results": [
                        {
                            "criterion_id": item["id"],
                            "status": "UNCONFIRMED",
                            "description": "由 V0.2 迁移，未提供员工逐项验收结论",
                            "evidence": [],
                        }
                        for item in criteria
                    ],
                }
            connection.execute(
                tasks.update()
                .where(tasks.c.id == row["id"])
                .values(
                    task_type=task_type,
                    category_id="testing-platform",
                    acceptance_criteria=criteria,
                    current_progress=progress,
                    result=result,
                )
            )

    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("acceptance_criteria", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("attachments", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("current_progress", existing_type=sa.Text(), nullable=False)

    priority_map = {"P0": "VERY_HIGH", "P1": "HIGH", "P2": "MEDIUM", "P3": "LOW"}
    for legacy, current in priority_map.items():
        connection.execute(tasks.update().where(tasks.c.priority == legacy).values(priority=current))
    status_map = {
        "CREATED": "READY",
        "CLAIMED": "RUNNING",
        "VERIFYING": "RUNNING",
        "RETRY": "RUNNING",
    }
    for legacy, current in status_map.items():
        connection.execute(tasks.update().where(tasks.c.status == legacy).values(status=current))

    with op.batch_alter_table("tasks") as batch:
        batch.drop_column("max_retry_count")
        batch.drop_column("retry_count")
        batch.drop_column("progress")
        batch.drop_column("current_phase")
        batch.drop_column("workspace")
        batch.drop_column("definition_of_done")
        batch.drop_column("input")
    with op.batch_alter_table("digital_employees") as batch:
        batch.drop_column("max_concurrency")
        batch.drop_column("auto_repair")
        batch.drop_column("source_scope")
        batch.drop_column("skills")
        batch.drop_column("capabilities")


def downgrade() -> None:
    tasks = sa.table(
        "tasks",
        sa.column("priority", sa.String()),
        sa.column("status", sa.String()),
    )
    connection = op.get_bind()
    for current, legacy in {
        "VERY_HIGH": "P0",
        "HIGH": "P1",
        "MEDIUM": "P2",
        "LOW": "P3",
    }.items():
        connection.execute(tasks.update().where(tasks.c.priority == current).values(priority=legacy))

    with op.batch_alter_table("digital_employees") as batch:
        batch.add_column(sa.Column("capabilities", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("skills", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("source_scope", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("auto_repair", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="1"))
    employees_for_defaults = sa.table(
        "digital_employees",
        sa.column("capabilities", sa.JSON()),
        sa.column("skills", sa.JSON()),
        sa.column("source_scope", sa.JSON()),
    )
    connection.execute(
        employees_for_defaults.update().values(
            capabilities=json_default([]),
            skills=json_default([]),
            source_scope=json_default([]),
        )
    )
    with op.batch_alter_table("digital_employees") as batch:
        batch.alter_column("capabilities", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("skills", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("source_scope", existing_type=sa.JSON(), nullable=False)

    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("input", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("definition_of_done", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("workspace", sa.Text(), nullable=True))
        batch.add_column(sa.Column("current_phase", sa.String(80), nullable=False, server_default="work"))
        batch.add_column(sa.Column("progress", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("max_retry_count", sa.Integer(), nullable=False, server_default="3"))

    legacy_tasks = sa.table(
        "tasks",
        sa.column("input", sa.JSON()),
        sa.column("definition_of_done", sa.JSON()),
    )
    connection.execute(
        legacy_tasks.update().values(
            input=json_default({}), definition_of_done=json_default([])
        )
    )
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("input", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("definition_of_done", existing_type=sa.JSON(), nullable=False)

    op.drop_index("ix_tasks_category_id", table_name="tasks")
    with op.batch_alter_table("tasks") as batch:
        batch.alter_column("priority", type_=sa.String(8), existing_type=sa.String(16))
        batch.drop_column("human_intervention")
        batch.drop_column("current_progress")
        batch.drop_column("attachments")
        batch.drop_column("acceptance_criteria")
        batch.drop_column("category_id")
    with op.batch_alter_table("digital_employees") as batch:
        batch.drop_column("category_ids")
        batch.drop_column("task_types")
    op.drop_table("task_categories")
