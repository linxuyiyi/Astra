# Nexus v1.5.0 数据库最终结构

从最终 ORM 元数据生成，共 52 张 Nexus 业务表；另有 Alembic 自身的 `alembic_version`。不新增业务表，移除原执行会话、实例、执行环境与连接历史四张表。

公司目标为全新 MySQL 8.0.36；由单个 `0001_init` 创建，不提供旧库升级或双写。AgentScope 自有数据库由官方维护，不在此清单内。

核心表：Task 27 列、AgentWorker 26 列、WorkerCommand 20 列、维护操作 24 列。`ssh_password` 与 `git_token_ciphertext` 均存密文；接口只返回是否已配置。

知识与证据表的 `source_session_id` / `session_id` 为 OpenCode 原生来源引用字符串，已移除到旧 Session 表的外键。旧 Session 状态快照字段已删除；来源 Job、消息、证据及人工审核机制保留。

## t_nexus_agent_workers（26 列）

`id`, `name`, `description`, `enabled`, `created_at`, `updated_at`, `status`, `task_types`, `category_ids`, `runtime_engine`, `mode`, `host`, `port`, `username`, `ssh_password`, `model_name`, `workspace_root`, `repository_url`, `git_token_ciphertext`, `branch`, `skill_refs`, `mcp_refs`, `token_digest`, `epoch`, `last_heartbeat_at`, `agent_version`

## t_nexus_attachments（7 列）

`id`, `owner_uid`, `name`, `media_type`, `size`, `path`, `created_at`

## t_nexus_channel_members（10 列）

`channel_id`, `employee_id`, `member_type`, `member_id`, `role`, `membership_active`, `read_seq`, `last_seen`, `agentscope_session_id`, `last_delivered_message_id`

## t_nexus_channels（11 列）

`id`, `name`, `description`, `next_seq`, `generation`, `lease_token`, `lease_until`, `record_agent_id`, `record_session_id`, `created_at`, `updated_at`

## t_nexus_conversations（9 列）

`id`, `employee_id`, `owner_id`, `kind`, `name`, `agentscope_session_id`, `workspace_id`, `created_at`, `updated_at`

## t_nexus_discussions（12 列）

`id`, `channel_id`, `message_id`, `initiator_id`, `generation`, `state`, `participants_json`, `turns`, `tokens`, `active_employee_id`, `detail`, `created_at`

## t_nexus_employees（17 列）

`id`, `name`, `domain`, `description`, `instructions`, `avatar`, `enabled`, `knowledge_json`, `skills_json`, `mcp_json`, `tools_json`, `agentscope_agent_id`, `mailbox_session_id`, `capability_json`, `model_route_id`, `created_at`, `updated_at`

## t_nexus_events（8 列）

`id`, `task_id`, `employee_id`, `event_type`, `phase`, `message`, `detail`, `created_at`

## t_nexus_experience_evidence（19 列）

`id`, `knowledge_id`, `task_id`, `source_session_id`, `message_id`, `part_id`, `file_path`, `diff_hash`, `evidence_type`, `content`, `uri`, `result`, `source_system`, `native_session_id`, `source_digest`, `review_job_id`, `validation_status`, `captured_at`, `created_at`

## t_nexus_experience_review_jobs（30 列）

`id`, `confirmation_event_id`, `review_key`, `task_id`, `task_id_snapshot`, `root_session_id`, `agent_worker_id_snapshot`, `turn_anchor_id`, `confirmed_at`, `source_snapshot`, `source_digest`, `status`, `phase`, `attempts`, `next_retry_at`, `lease_owner`, `lease_token`, `lease_until`, `source_message_count`, `segment_count`, `completed_segment_count`, `candidate_ids`, `zero_reason`, `error_code`, `error_message`, `model_id`, `prompt_version`, `created_at`, `updated_at`, `finished_at`

## t_nexus_experience_review_segments（15 列）

`id`, `job_id`, `segment_index`, `source_refs`, `input_digest`, `token_estimate`, `status`, `attempts`, `lease_token`, `lease_until`, `next_retry_at`, `analysis_json`, `error_code`, `created_at`, `updated_at`

## t_nexus_gateway_audit_events（10 列）

`id`, `actor`, `actor_role`, `action`, `resource_type`, `resource_id`, `result`, `before`, `after`, `created_at`

## t_nexus_gateway_health_checks（9 列）

`id`, `node_id`, `probe_type`, `success`, `latency_ms`, `status_code`, `error`, `detail`, `created_at`

## t_nexus_gateway_keys（16 列）

`id`, `name`, `key_prefix`, `key_digest`, `owner_id`, `allowed_models`, `max_concurrency`, `hourly_token_limit`, `daily_token_limit`, `enabled`, `expires_at`, `last_used_at`, `revoked_at`, `deleted_at`, `created_at`, `updated_at`

## t_nexus_gateway_nodes（28 列）

`id`, `name`, `pool_id`, `base_url`, `api_key_encrypted`, `auth_type`, `auth_header`, `connect_timeout_seconds`, `request_timeout_seconds`, `health_path`, `probe_strategy`, `probe_interval_seconds`, `models`, `weight`, `max_concurrency`, `hourly_token_limit`, `daily_token_limit`, `quota_policy`, `config_state`, `scheduling_state`, `health_state`, `success_streak`, `failure_streak`, `health_detail`, `sort_order`, `last_checked_at`, `created_at`, `updated_at`

## t_nexus_gateway_pools（7 列）

`id`, `name`, `description`, `enabled`, `sort_order`, `created_at`, `updated_at`

## t_nexus_gateway_probe_runs（16 列）

`id`, `node_id`, `source`, `strategy`, `first_model`, `runtime_scope`, `success`, `complete`, `status`, `steps`, `usage`, `error`, `started_at`, `finished_at`, `duration_ms`, `created_at`

## t_nexus_gateway_requests（26 列）

`id`, `key_id`, `owner_id`, `model`, `actual_model`, `pool_id`, `node_id`, `streaming`, `retry_count`, `status_code`, `error_code`, `stream_interrupted`, `connect_ms`, `ttfb_ms`, `ttft_ms`, `duration_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `output_tps`, `started_at`, `finished_at`, `gateway_instance_id`, `gateway_instance_started_at`, `outcome`, `created_at`

## t_nexus_gateway_routes（9 列）

`id`, `model`, `model_type`, `pool_id`, `target_model`, `description`, `enabled`, `created_at`, `updated_at`

## t_nexus_gateway_runtime_events（11 列）

`id`, `source`, `kind`, `node_id`, `pool_id`, `model`, `summary`, `previous`, `current`, `detail`, `created_at`

## t_nexus_gateway_speech_input（2 列）

`id`, `route_id`

## t_nexus_group_audit（6 列）

`id`, `channel_id`, `actor_id`, `action`, `target_id`, `created_at`

## t_nexus_group_events（4 列）

`id`, `channel_id`, `payload_json`, `created_at`

## t_nexus_group_invites（5 列）

`token_hash`, `channel_id`, `created_by`, `expires_at`, `used_by`

## t_nexus_identity_profiles（2 列）

`id`, `display_name`

## t_nexus_im_bindings（5 列）

`employee_id`, `kind`, `external_key`, `owner_uid`, `session_id`

## t_nexus_im_bots（3 列）

`employee_id`, `provider`, `bot_id`

## t_nexus_im_events（7 列）

`provider`, `event_id`, `employee_id`, `session_id`, `accepted`, `claim_token`, `claim_until`

## t_nexus_im_outbox（8 列）

`delivery_id`, `employee_id`, `conversation_key`, `session_id`, `body`, `state`, `lease_until`, `created_at`

## t_nexus_im_threads（3 列）

`employee_id`, `conversation_key`, `session_id`

## t_nexus_knowledge_audit（7 列）

`id`, `knowledge_id`, `action`, `actor_id`, `actor_role`, `detail`, `created_at`

## t_nexus_knowledge_favorites（4 列）

`id`, `knowledge_id`, `user_id`, `created_at`

## t_nexus_knowledge_feedback（12 列）

`id`, `resource_id`, `knowledge_id`, `actor_key`, `user_id`, `employee_id`, `feedback_type`, `comment`, `task_id`, `status`, `created_at`, `resolved_at`

## t_nexus_knowledge_items（40 列）

`id`, `space_id`, `title`, `type`, `summary`, `content_md`, `status`, `sync_status`, `sync_error`, `synced_at`, `source_path`, `verification_status`, `tags`, `source_type`, `source_ref`, `source_employee_id`, `source_task_id`, `source_session_id`, `origin`, `dedupe_key`, `fingerprint`, `experience_type`, `scope`, `verification_message_id`, `verified_at`, `source_ref_snapshot`, `experience_data`, `review_note`, `created_by`, `updated_by`, `published_by`, `published_at`, `agentscope_kb_id`, `agentscope_document_id`, `agentscope_sync_status`, `agentscope_sync_error`, `reference_count`, `helpful_count`, `created_at`, `updated_at`

## t_nexus_knowledge_relations（5 列）

`id`, `source_knowledge_id`, `target_knowledge_id`, `relation_type`, `created_at`

## t_nexus_knowledge_sources（8 列）

`id`, `knowledge_id`, `source_type`, `source_id`, `source_name`, `source_uri`, `version`, `created_at`

## t_nexus_knowledge_spaces（10 列）

`id`, `name`, `description`, `parent_id`, `sort_order`, `status`, `access_scope`, `created_by`, `created_at`, `updated_at`

## t_nexus_knowledge_usage（12 列）

`id`, `resource_id`, `knowledge_id`, `employee_id`, `task_id`, `session_id`, `tool_name`, `action`, `query`, `result`, `latency_ms`, `created_at`

## t_nexus_mcps（15 列）

`id`, `name`, `description`, `url`, `transport`, `headers_json`, `auth_type`, `auth_header`, `token_ciphertext`, `timeout`, `runtime_name`, `enabled`, `revision`, `created_at`, `updated_at`

## t_nexus_messages（15 列）

`id`, `conversation_id`, `channel_id`, `sender`, `text`, `attachments_json`, `accepted`, `seq`, `sender_type`, `sender_name`, `native_json`, `discussion_id`, `client_id`, `speaker_uid`, `created_at`

## t_nexus_peer_deliveries（6 列）

`id`, `scope`, `sender_agent_id`, `target_agent_id`, `message_key`, `created_at`

## t_nexus_resource_sync（8 列）

`resource_type`, `resource_id`, `employee_id`, `session_id`, `status`, `detail`, `revision`, `updated_at`

## t_nexus_settings（2 列）

`key`, `value`

## t_nexus_skill_versions（6 列）

`id`, `skill_id`, `version`, `note`, `files_json`, `created_at`

## t_nexus_skills（10 列）

`id`, `name`, `description`, `version`, `revision`, `runtime_name`, `files_json`, `enabled`, `created_at`, `updated_at`

## t_nexus_task_categories（9 列）

`id`, `name`, `description`, `task_prompt`, `question_prompt`, `enabled`, `sort_order`, `created_at`, `updated_at`

## t_nexus_task_wake_outbox（13 列）

`id`, `event_id`, `task_id`, `origin_employee_id`, `source`, `external_task_id`, `status`, `hint`, `attempts`, `next_retry_at`, `last_error`, `created_at`, `updated_at`

## t_nexus_tasks（27 列）

`id`, `title`, `description`, `task_type`, `category_id`, `priority`, `acceptance_criteria`, `attachments`, `source`, `external_task_id`, `origin_employee_id`, `external_reference_id`, `agent_worker_id`, `current_session_id`, `workspace`, `last_message_id`, `pending_message_id`, `pending_prompt`, `status`, `waiting_reason`, `current_progress`, `created_at`, `updated_at`, `claimed_at`, `started_at`, `finished_at`, `last_activity_at`

## t_nexus_worker_commands（20 列）

`id`, `request_id`, `agent_worker_id`, `session_id`, `name`, `payload`, `status`, `epoch`, `result`, `error_code`, `deadline_at`, `body_fetched_at`, `delivery_attempts`, `delivery_state`, `slot_held`, `last_dispatched_at`, `dispatched_epoch`, `created_at`, `updated_at`, `finished_at`

## t_nexus_worker_http_slots（3 列）

`agent_worker_id`, `epoch`, `inflight`

## t_nexus_worker_maintenance_events（10 列）

`id`, `operation_id`, `attempt`, `type`, `stage`, `name`, `state`, `message`, `detail`, `created_at`

## t_nexus_worker_maintenance_operations（24 列）

`id`, `agent_worker_id`, `action`, `state`, `stage`, `requested_by`, `attempt`, `lease_owner`, `lease_until`, `cancel_requested`, `target_revision`, `before_snapshot`, `after_snapshot`, `error_code`, `error_message`, `execution_channel`, `execution_reason`, `retry_of_attempt`, `active_worker_id`, `requested_at`, `started_at`, `finished_at`, `created_at`, `updated_at`
