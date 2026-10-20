"""API 中转控制面：节点、资源池、路由、Key、版本、健康、日志与审计。"""

import sqlalchemy as sa

from alembic import op

revision = "0031_api_gateway_control"
down_revision = "0030_assistant_task_reviews"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "gateway_pools",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("name", name="uq_gateway_pools_name"),
    )
    op.create_index("ix_gateway_pools_enabled", "gateway_pools", ["enabled"])
    op.create_index("ix_gateway_pools_sort_order", "gateway_pools", ["sort_order"])

    op.create_table(
        "gateway_nodes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("pool_id", sa.String(64), sa.ForeignKey("gateway_pools.id", ondelete="SET NULL")),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("api_key_encrypted", sa.Text()),
        sa.Column("auth_type", sa.String(16), nullable=False),
        sa.Column("auth_header", sa.String(64), nullable=False),
        sa.Column("connect_timeout_seconds", sa.Float()),
        sa.Column("request_timeout_seconds", sa.Float()),
        sa.Column("health_path", sa.String(256), nullable=False),
        sa.Column("probe_interval_seconds", sa.Integer()),
        sa.Column("models", sa.JSON(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("hourly_token_limit", sa.Integer()),
        sa.Column("daily_token_limit", sa.Integer()),
        sa.Column("quota_policy", sa.String(16), nullable=False),
        sa.Column("config_state", sa.String(16), nullable=False),
        sa.Column("scheduling_state", sa.String(16), nullable=False),
        sa.Column("health_state", sa.String(16), nullable=False),
        sa.Column("success_streak", sa.Integer(), nullable=False),
        sa.Column("failure_streak", sa.Integer(), nullable=False),
        sa.Column("health_detail", sa.JSON(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gateway_nodes_pool_id", "gateway_nodes", ["pool_id"])
    op.create_index("ix_gateway_nodes_config_state", "gateway_nodes", ["config_state"])
    op.create_index("ix_gateway_nodes_scheduling_state", "gateway_nodes", ["scheduling_state"])
    op.create_index("ix_gateway_nodes_health_state", "gateway_nodes", ["health_state"])
    op.create_index("ix_gateway_nodes_sort_order", "gateway_nodes", ["sort_order"])

    op.create_table(
        "gateway_routes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column(
            "pool_id",
            sa.String(64),
            sa.ForeignKey("gateway_pools.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("model", name="uq_gateway_routes_model"),
    )
    op.create_index("ix_gateway_routes_pool_id", "gateway_routes", ["pool_id"])
    op.create_index("ix_gateway_routes_enabled", "gateway_routes", ["enabled"])

    op.create_table(
        "gateway_keys",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("owner_id", sa.String(120)),
        sa.Column("allowed_models", sa.JSON(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("hourly_token_limit", sa.Integer()),
        sa.Column("daily_token_limit", sa.Integer()),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("key_digest", name="uq_gateway_keys_digest"),
    )
    op.create_index("ix_gateway_keys_key_prefix", "gateway_keys", ["key_prefix"])
    op.create_index("ix_gateway_keys_owner_id", "gateway_keys", ["owner_id"])
    op.create_index("ix_gateway_keys_enabled", "gateway_keys", ["enabled"])

    op.create_table(
        "gateway_config_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_version", sa.Integer()),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("published_by", sa.String(120), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("version", name="uq_gateway_config_versions_version"),
    )
    op.create_index("ix_gateway_config_versions_status", "gateway_config_versions", ["status"])
    op.create_index(
        "ix_gateway_config_versions_published_at", "gateway_config_versions", ["published_at"]
    )

    op.create_table(
        "gateway_health_checks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            sa.String(64),
            sa.ForeignKey("gateway_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("probe_type", sa.String(16), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("error", sa.Text()),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gateway_health_checks_node_id", "gateway_health_checks", ["node_id"])
    op.create_index("ix_gateway_health_checks_probe_type", "gateway_health_checks", ["probe_type"])
    op.create_index(
        "ix_gateway_health_node_time", "gateway_health_checks", ["node_id", "created_at"]
    )

    op.create_table(
        "gateway_requests",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("key_id", sa.String(64)),
        sa.Column("owner_id", sa.String(120)),
        sa.Column("model", sa.String(200)),
        sa.Column("pool_id", sa.String(64)),
        sa.Column("node_id", sa.String(64)),
        sa.Column("streaming", sa.Boolean(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("stream_interrupted", sa.Boolean(), nullable=False),
        sa.Column("connect_ms", sa.Integer()),
        sa.Column("ttfb_ms", sa.Integer()),
        sa.Column("ttft_ms", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("output_tps", sa.Float()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gateway_requests_time", "gateway_requests", ["started_at"])
    op.create_index("ix_gateway_requests_key_time", "gateway_requests", ["key_id", "started_at"])
    op.create_index("ix_gateway_requests_node_time", "gateway_requests", ["node_id", "started_at"])
    op.create_index("ix_gateway_requests_error_code", "gateway_requests", ["error_code"])
    op.create_index("ix_gateway_requests_owner_id", "gateway_requests", ["owner_id"])
    op.create_index("ix_gateway_requests_node_id", "gateway_requests", ["node_id"])

    op.create_table(
        "gateway_audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("actor_role", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(64)),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("before", sa.JSON(), nullable=False),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_gateway_audit_events_actor", "gateway_audit_events", ["actor"])
    op.create_index("ix_gateway_audit_events_action", "gateway_audit_events", ["action"])
    op.create_index("ix_gateway_audit_events_resource_type", "gateway_audit_events", ["resource_type"])
    op.create_index("ix_gateway_audit_events_resource_id", "gateway_audit_events", ["resource_id"])
    op.create_index("ix_gateway_audit_events_result", "gateway_audit_events", ["result"])
    op.create_index("ix_gateway_audit_events_created_at", "gateway_audit_events", ["created_at"])
    op.create_index(
        "ix_gateway_audit_resource", "gateway_audit_events", ["resource_type", "resource_id"]
    )


def downgrade():
    op.drop_table("gateway_audit_events")
    op.drop_table("gateway_requests")
    op.drop_table("gateway_health_checks")
    op.drop_table("gateway_config_versions")
    op.drop_table("gateway_keys")
    op.drop_table("gateway_routes")
    op.drop_table("gateway_nodes")
    op.drop_table("gateway_pools")