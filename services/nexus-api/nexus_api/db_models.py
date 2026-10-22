"""Nexus 员工、会话绑定和资源目录的当前 SQL schema。"""

from sqlalchemy import Column, Float, Index, Integer, String, Table, Text, UniqueConstraint, text
from sqlalchemy.dialects.mysql import LONGTEXT, VARCHAR

from app.models import Base


def identifier(length: int = 160):
    return String(length).with_variant(VARCHAR(length, collation="utf8mb4_bin"), "mysql")


CONTENT = Text().with_variant(LONGTEXT(), "mysql")


employees = Table(
    "t_nexus_employees",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("name", identifier(255), nullable=False),
    Column("domain", identifier(255), nullable=False),
    Column("description", CONTENT, nullable=False),
    Column("instructions", CONTENT, nullable=False),
    Column("avatar", identifier(255), nullable=False),
    Column("enabled", Integer, nullable=False),
    Column("knowledge_json", CONTENT, nullable=False),
    Column("skills_json", CONTENT, nullable=False),
    Column("mcp_json", CONTENT, nullable=False, server_default=text("('[]')")),
    Column("tools_json", CONTENT, nullable=False),
    Column("agentscope_agent_id", identifier(255), nullable=True),
    Column("mailbox_session_id", identifier(255), nullable=True),
    Column("capability_json", CONTENT, nullable=False, server_default=text("('{}')")),
    Column("model_route_id", identifier(64), nullable=False, server_default=text("''")),
    Column("created_at", identifier(40), nullable=False),
    Column("updated_at", identifier(40), nullable=False),
)

conversations = Table(
    "t_nexus_conversations",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("employee_id", identifier(64), nullable=False),
    Column("owner_id", identifier(255), nullable=False),
    Column("kind", identifier(32), nullable=False, server_default=text("'private'")),
    Column("name", identifier(255), nullable=False),
    Column("agentscope_session_id", identifier(255), nullable=True),
    Column("workspace_id", identifier(255), nullable=False, server_default=text("''")),
    Column("created_at", identifier(40), nullable=False),
    Column("updated_at", identifier(40), nullable=False),
    Index("ux_private_conversation", "owner_id", "employee_id", "kind", unique=True),
)

messages = Table(
    "t_nexus_messages",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("conversation_id", identifier(64), nullable=True),
    Column("channel_id", identifier(64), nullable=True),
    Column("sender", identifier(160), nullable=False),
    Column("text", CONTENT, nullable=False),
    Column("attachments_json", CONTENT, nullable=False),
    Column("accepted", Integer, nullable=False, server_default=text("0")),
    Column("seq", Integer, nullable=True),
    Column("sender_type", identifier(16), nullable=False, server_default=text("'human'")),
    Column("sender_name", identifier(255), nullable=False, server_default=text("''")),
    Column("native_json", CONTENT, nullable=False, server_default=text("('{}')")),
    Column("discussion_id", identifier(64), nullable=False, server_default=text("''")),
    Column("client_id", identifier(64), nullable=True),
    Column("speaker_uid", identifier(160), nullable=False, server_default=text("''")),
    Column("created_at", identifier(40), nullable=False),
    UniqueConstraint("channel_id", "seq", name="uq_group_seq"),
    UniqueConstraint("channel_id", "speaker_uid", "client_id", name="uq_group_client"),
)

channels = Table(
    "t_nexus_channels",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("name", identifier(255), nullable=False),
    Column("description", CONTENT, nullable=False),
    Column("next_seq", Integer, nullable=False, server_default=text("0")),
    Column("generation", Integer, nullable=False, server_default=text("0")),
    Column("lease_token", identifier(64), nullable=False, server_default=text("''")),
    Column("lease_until", identifier(40), nullable=False, server_default=text("''")),
    Column("record_agent_id", identifier(255), nullable=False, server_default=text("''")),
    Column("record_session_id", identifier(255), nullable=False, server_default=text("''")),
    Column("created_at", identifier(40), nullable=False),
    Column("updated_at", identifier(40), nullable=False),
)

channel_members = Table(
    "t_nexus_channel_members",
    Base.metadata,
    Column("channel_id", identifier(64), primary_key=True),
    Column("employee_id", identifier(64), nullable=False, server_default=text("''")),
    Column("member_type", identifier(16), primary_key=True),
    Column("member_id", identifier(160), primary_key=True),
    Column("role", identifier(16), nullable=False, server_default=text("'member'")),
    Column("membership_active", Integer, nullable=False, server_default=text("1")),
    Column("read_seq", Integer, nullable=False, server_default=text("0")),
    Column("last_seen", identifier(40), nullable=False, server_default=text("''")),
    Column("agentscope_session_id", identifier(255), nullable=True),
    Column("last_delivered_message_id", identifier(64), nullable=False, server_default=text("''")),
)

attachments = Table(
    "t_nexus_attachments",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("owner_uid", identifier(160), nullable=False, server_default=text("''")),
    Column("name", identifier(255), nullable=False),
    Column("media_type", identifier(160), nullable=False),
    Column("size", Integer, nullable=False),
    Column("path", CONTENT, nullable=False),
    Column("created_at", identifier(40), nullable=False),
)

im_bots = Table(
    "t_nexus_im_bots",
    Base.metadata,
    Column("employee_id", identifier(64), primary_key=True),
    Column("provider", identifier(32), nullable=False),
    Column("bot_id", identifier(255), nullable=False),
    UniqueConstraint("provider", "bot_id", name="uq_nexus_im_bots"),
)

im_events = Table(
    "t_nexus_im_events",
    Base.metadata,
    Column("provider", identifier(32), primary_key=True),
    Column("event_id", identifier(255), primary_key=True),
    Column("employee_id", identifier(64), nullable=False),
    Column("session_id", identifier(255), nullable=False, server_default=text("''")),
    Column("accepted", Integer, nullable=False, server_default=text("0")),
    Column("claim_token", identifier(160), nullable=False, server_default=text("''")),
    Column("claim_until", identifier(40), nullable=False, server_default=text("''")),
)

im_bindings = Table(
    "t_nexus_im_bindings",
    Base.metadata,
    Column("employee_id", identifier(64), primary_key=True),
    Column("kind", identifier(32), primary_key=True),
    Column("external_key", identifier(255), primary_key=True, server_default=text("''")),
    Column("owner_uid", identifier(160), primary_key=True, server_default=text("''")),
    Column("session_id", identifier(255), nullable=False),
)

im_threads = Table(
    "t_nexus_im_threads",
    Base.metadata,
    Column("employee_id", identifier(64), primary_key=True),
    Column("conversation_key", identifier(255), primary_key=True),
    Column("session_id", identifier(255), nullable=False),
)

im_outbox = Table(
    "t_nexus_im_outbox",
    Base.metadata,
    Column("delivery_id", identifier(64), primary_key=True),
    Column("employee_id", identifier(64), nullable=False),
    Column("conversation_key", identifier(255), nullable=False),
    Column("session_id", identifier(255), nullable=False),
    Column("body", CONTENT, nullable=False),
    Column("state", identifier(32), nullable=False),
    Column("lease_until", identifier(40), nullable=False, server_default=text("''")),
    Column("created_at", identifier(40), nullable=False),
)

peer_deliveries = Table(
    "t_nexus_peer_deliveries",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("scope", identifier(160), nullable=False),
    Column("sender_agent_id", identifier(255), nullable=False),
    Column("target_agent_id", identifier(255), nullable=False),
    Column("message_key", Text().with_variant(LONGTEXT(collation="utf8mb4_bin"), "mysql"), nullable=False),
    Column("created_at", identifier(40), nullable=False),
)

skills = Table(
    "t_nexus_skills",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("name", identifier(255), nullable=False),
    Column("description", CONTENT, nullable=False),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("revision", Integer, nullable=False, server_default=text("1")),
    Column("runtime_name", identifier(255), nullable=False),
    Column("files_json", CONTENT, nullable=False, server_default=text("('[]')")),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column("created_at", identifier(40), nullable=False),
    Column("updated_at", identifier(40), nullable=False),
    Index("ux_nexus_skills_name", "name", unique=True),
)

skill_versions = Table(
    "t_nexus_skill_versions",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("skill_id", identifier(64), nullable=False),
    Column("version", Integer, nullable=False),
    Column("note", CONTENT, nullable=False, server_default=text("('')")),
    Column("files_json", CONTENT, nullable=False, server_default=text("('[]')")),
    Column("created_at", identifier(40), nullable=False),
    UniqueConstraint("skill_id", "version", name="uq_nexus_skill_versions"),
)

mcps = Table(
    "t_nexus_mcps",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("name", identifier(255), nullable=False),
    Column("description", CONTENT, nullable=False, server_default=text("('')")),
    Column("url", CONTENT, nullable=False),
    Column("transport", identifier(32), nullable=False, server_default=text("'http'")),
    Column("headers_json", CONTENT, nullable=False, server_default=text("('[]')")),
    Column("auth_type", identifier(32), nullable=False, server_default=text("'none'")),
    Column("auth_header", identifier(160), nullable=False, server_default=text("'Authorization'")),
    Column("token_ciphertext", CONTENT, nullable=False, server_default=text("('')")),
    Column("timeout", Float, nullable=False, server_default=text("30")),
    Column("runtime_name", identifier(255), nullable=False),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column("revision", Integer, nullable=False, server_default=text("1")),
    Column("created_at", identifier(40), nullable=False),
    Column("updated_at", identifier(40), nullable=False),
    Index("ux_nexus_mcps_runtime_name", "runtime_name", unique=True),
    Index("ux_nexus_mcps_name", "name", unique=True),
)

resource_sync = Table(
    "t_nexus_resource_sync",
    Base.metadata,
    Column("resource_type", identifier(32), primary_key=True),
    Column("resource_id", identifier(64), primary_key=True),
    Column("employee_id", identifier(64), primary_key=True),
    Column("session_id", identifier(255), primary_key=True),
    Column("status", identifier(32), nullable=False, server_default=text("'pending'")),
    Column("detail", CONTENT, nullable=False, server_default=text("('')")),
    Column("revision", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", identifier(40), nullable=False),
)

settings = Table(
    "t_nexus_settings",
    Base.metadata,
    Column("key", identifier(255), primary_key=True),
    Column("value", CONTENT, nullable=False),
)

users = Table(
    "t_nexus_users",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("username", identifier(64), nullable=False, unique=True),
    Column("display_name", identifier(64), nullable=False),
    Column("password_hash", identifier(255), nullable=False),
    Column("permissions_json", CONTENT, nullable=False),
    Column("enabled", Integer, nullable=False, server_default=text("1")),
    Column("created_at", identifier(40), nullable=False),
)
auth_sessions = Table(
    "t_nexus_auth_sessions",
    Base.metadata,
    Column("token_hash", identifier(64), primary_key=True),
    Column("user_id", identifier(64), nullable=False),
    Column("expires_at", identifier(40), nullable=False),
)
group_invites = Table(
    "t_nexus_group_invites",
    Base.metadata,
    Column("token_hash", identifier(64), primary_key=True),
    Column("channel_id", identifier(64), nullable=False),
    Column("created_by", identifier(64), nullable=False),
    Column("expires_at", identifier(40), nullable=False),
    Column("used_by", identifier(64), nullable=False, server_default=text("''")),
)
discussions = Table(
    "t_nexus_discussions",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("channel_id", identifier(64), nullable=False, index=True),
    Column("message_id", identifier(64), nullable=False, unique=True),
    Column("initiator_id", identifier(64), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("state", identifier(32), nullable=False),
    Column("participants_json", CONTENT, nullable=False),
    Column("turns", Integer, nullable=False, server_default=text("0")),
    Column("tokens", Integer, nullable=False, server_default=text("0")),
    Column("active_employee_id", identifier(64), nullable=False, server_default=text("''")),
    Column("detail", CONTENT, nullable=False, server_default=text("('')")),
    Column("created_at", identifier(40), nullable=False),
)
group_events = Table(
    "t_nexus_group_events",
    Base.metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("channel_id", identifier(64), nullable=False, index=True),
    Column("payload_json", CONTENT, nullable=False),
    Column("created_at", identifier(40), nullable=False),
)
group_audit = Table(
    "t_nexus_group_audit",
    Base.metadata,
    Column("id", identifier(64), primary_key=True),
    Column("channel_id", identifier(64), nullable=False),
    Column("actor_id", identifier(64), nullable=False),
    Column("action", identifier(64), nullable=False),
    Column("target_id", identifier(160), nullable=False),
    Column("created_at", identifier(40), nullable=False),
)
