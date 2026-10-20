"""Prefix every task-platform table with ``t_nexus_``.

Nexus and the former task platform now share one SQLite/MySQL database, so all
business tables use the product prefix. Index names are left untouched: they
are not part of the external contract and renaming them would not change
behaviour.
"""

from alembic import op

revision = "0052_t_nexus_table_prefix"
down_revision = "0051_rename_workers_table"
branch_labels = None
depends_on = None

TABLES = (
    "agent_workers",
    "assistant_context_refs",
    "assistant_conversations",
    "assistant_message_parts",
    "assistant_messages",
    "assistant_operations",
    "assistant_tool_calls",
    "code_repositories",
    "events",
    "experience_evidence",
    "gateway_audit_events",
    "gateway_health_checks",
    "gateway_keys",
    "gateway_nodes",
    "gateway_pools",
    "gateway_requests",
    "gateway_routes",
    "gateway_runtime_events",
    "git_credentials",
    "knowledge_audit",
    "knowledge_favorites",
    "knowledge_feedback",
    "knowledge_items",
    "knowledge_relations",
    "knowledge_sources",
    "knowledge_spaces",
    "knowledge_usage",
    "opencode_execution_environments",
    "runtime_sessions",
    "task_categories",
    "task_reviews",
    "task_summaries",
    "task_wake_outbox",
    "tasks",
    "worker_commands",
    "worker_connections",
    "worker_http_slots",
    "worker_instances",
    "worker_maintenance_events",
    "worker_maintenance_operations",
)


def upgrade():
    for table in TABLES:
        op.rename_table(table, f"t_nexus_{table}")


def downgrade():
    for table in TABLES:
        op.rename_table(f"t_nexus_{table}", table)
