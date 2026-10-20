export interface RuntimeProjection {
  runtime_state: string
  runtime_status_label: string
  runtime_detail: string
  failure: { failure_code: string; failure_scope: string; retryable: boolean; recovery_action: string; raw_error: string } | null
}

export type TaskType = 'ISSUE' | 'REQUIREMENT'
export type TaskPriority = 'VERY_HIGH' | 'HIGH' | 'MEDIUM' | 'LOW'
export type TaskStatus = import('@/features/runtime/status').TaskState
export type CompletionPolicy = 'AUTO' | 'MANUAL_CONFIRM'
export type AcceptanceStatus = 'SATISFIED' | 'UNSATISFIED' | 'UNCONFIRMED'

export interface TaskCategory {
  id: string
  name: string
  description: string
  task_prompt: string | null
  question_prompt: string | null
  enabled: boolean
  sort_order: number
  created_at: string
  updated_at: string
}

export type TaskCategoryWrite = Pick<TaskCategory, 'name' | 'description' | 'enabled' | 'sort_order' | 'task_prompt' | 'question_prompt'>

export interface ExecutionEnvironment {
  id: number
  employee_id: string
  mode: 'LOCAL' | 'LINUX_REMOTE'
  name: string
  description: string
  host: string | null
  port: number
  username: string | null
  has_ssh_password: boolean
  opencode_server_port: number
  opencode_path: string
  model_name: string | null
  workspace_root: string
  repository_id?: string | null
  repository_url?: string | null
  repository_branch?: string | null
  branch?: string | null
  status: 'UNKNOWN' | 'CHECKING' | 'AVAILABLE' | 'UNAVAILABLE'
  status_detail: Record<string, unknown>
  labels: string[]
  last_checked_at: string | null
  created_at: string
  updated_at: string
}

export interface UsageSummary {
  input_tokens: number | null
  output_tokens: number | null
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  total_tokens: number | null
  llm_call_count: number | null
  tool_call_count: number | null
  tool_success_count: number
  tool_failure_count: number
  duration_seconds: number
  execution_seconds: number | null
  idle_seconds: number | null
  waiting_seconds: number
  completed_tasks: number
  task_count: number
  session_count: number
  native_session_count: number
  completed_sessions: number
  failed_sessions: number
  interrupted_sessions: number
}

export interface ToolUsage {
  tool_name: string
  call_count: number
  success_count: number
  failure_count: number
  duration_ms: number
}

export interface ModelUsage {
  model_name: string
  session_count: number
  total_tokens: number
  llm_call_count: number
  tool_call_count: number
}

export interface EmployeeUsage {
  period_days: number
  summary: UsageSummary
  trend: Array<{ date: string } & UsageSummary>
  tools: ToolUsage[]
  models?: ModelUsage[]
  token_by_day?: Array<{ date: string } & UsageSummary>
}

export interface TaskUsage { summary: UsageSummary; tools: ToolUsage[]; sessions: number }

export interface CodeRepository {
  id: string
  url: string
  branch: string
}

export interface Employee extends RuntimeProjection {
  id: string
  digital_employee_id: string | null
  name: string
  role: string
  description: string
  character_id: number
  default_work_area: 'development' | 'collaboration' | 'pantry' | 'integration' | 'meeting' | 'special'
  status: string
  task_types: TaskType[]
  category_ids: string[]
  enabled: boolean
  auto_claim: boolean
  auto_execute: boolean
  current_task_count: number
  last_heartbeat: string | null
  online_since: string | null
  created_at: string
  updated_at: string
  execution_environment: ExecutionEnvironment | null
  channel_health: {
    worker_online: boolean
    opencode_reachable: boolean
    repository_bound: boolean
    executing: boolean
    waiting_human: boolean
    availability: string
  }
  today_usage: UsageSummary
  today_completed: number
}

export interface AcceptanceCriterion { id: string; content: string }

export interface HumanIntervention {
  blocking_reason: string
  completed_work: string
  current_state: string
  requested_input: string
  can_suspend?: boolean
  scene_context?: string
}

export interface Task extends RuntimeProjection {
  id: string
  source: string
  external_task_id: string | null
  task_type: TaskType
  category_id: string | null
  title: string
  description: string
  acceptance_criteria: AcceptanceCriterion[]
  attachments: Array<Record<string, unknown>>
  priority: TaskPriority
  completion_policy: CompletionPolicy
  status: TaskStatus
  employee_id: string | null
  worker_id: string | null
  responsible_employee_id: string | null
  source_session_id: string | null
  source_conversation_id: string | null
  source_channel_id: string | null
  current_session_id: string | null
  session_status?: string | null
  session_terminal_reason?: string | null
  workspace: string | null
  runtime_connection: string
  pending_message_id: string | null
  runtime_error: string | null
  current_progress: string
  failure_reason: string | null
  human_required_reason: string | null
  human_intervention: HumanIntervention | null
  suspension: Record<string, unknown> | null
  execution_error: Record<string, unknown> | null
  inactivity_alert: boolean
  inactivity_alert_since: string | null
  last_activity_at: string | null
  submitted_at: string | null
  confirmed_at: string | null
  execution_environment_snapshot: Record<string, unknown> | null
  claimed_at: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export interface AstraEvent {
  id: number
  task_id: string | null
  employee_id: string | null
  event_type: string
  phase: string
  message: string
  detail: Record<string, unknown>
  created_at: string
}


export type KnowledgeType = 'document' | 'rule' | 'guide' | 'experience' | 'faq' | 'tool' | 'example'
export type KnowledgeStatus = 'draft' | 'candidate' | 'published' | 'archived' | 'rejected'
export type VerificationStatus = 'unverified' | 'verified' | 'failed'

export interface LibraryPermissions {
  user_id: string
  role: 'viewer' | 'maintainer' | 'reviewer' | 'admin'
  can_manage: boolean
  can_review: boolean
}

export interface LibraryStats {
  knowledge_pages: number
  visible_knowledge_total: number
  knowledge_spaces: number
  weekly_references: number
  published_experiences: number
  candidate_experiences: number
}

export interface KnowledgeSpace {
  id: string
  name: string
  description: string
  parent_id: string | null
  sort_order: number
  status: string
  access_scope: string
  knowledge_count: number
  subtree_knowledge_count: number
  children: KnowledgeSpace[]
  created_at: string
  updated_at: string
}

export interface KnowledgeUsageRecord {
  id: number
  employee_id: string | null
  employee_name: string | null
  task_id: string | null
  task_title: string | null
  action: string
  used_at: string
}

export interface KnowledgeListItem {
  id: string
  space_id: string | null
  space_name: string | null
  title: string
  type: KnowledgeType
  summary: string
  status: KnowledgeStatus
  verification_status: VerificationStatus
  tags: string[]
  source_type: string | null
  source_task_id: string | null
  reference_count: number
  helpful_count: number
  updated_at: string
  published_at: string | null
}

export interface KnowledgeSource {
  id: number
  source_type: string
  source_id: string | null
  source_name: string | null
  source_uri: string | null
  version: string | null
  created_at: string
}

export interface ExperienceEvidence {
  id: number
  task_id: string | null
  source_session_id: string | null
  message_id: string | null
  part_id: string | null
  file_path: string | null
  diff_hash: string | null
  evidence_type: string
  content: string | null
  uri: string | null
  result: string | null
  created_at: string
}

export interface KnowledgeDetail extends KnowledgeListItem {
  content_md: string
  source_type: string | null
  source_ref: string | null
  source_employee_id: string | null
  source_employee_name: string | null
  source_task_title: string | null
  source_session_id: string | null
  experience_type: string | null
  verified_at: string | null
  verification_session_status: string | null
  experience_data: Record<string, unknown>
  review_note: string | null
  created_by: string
  updated_by: string
  published_by: string | null
  created_at: string
  sources: KnowledgeSource[]
  evidence: ExperienceEvidence[]
  is_favorite: boolean
  user_helpful: boolean
}

export interface KnowledgePage {
  items: KnowledgeListItem[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface UsageTopKnowledge {
  resource_id: string
  title: string
  reference_count: number
}

export interface LibraryUsageSummary {
  days: number
  total_references: number
  used_resources: number
  feedback_count: number
  top_knowledge: UsageTopKnowledge[]
  trend: Array<{ date: string; reference_count: number }>
}

export interface RelatedTask {
  id: string
  title: string
  status: string
  relation: string
  last_used_at: string | null
}

export interface Metrics {
  online_employees: number
  running_tasks: number
  idle_employees: number
  error_employees: number
  pending_tasks: number
  waiting_tasks: number
  failed_tasks: number
  completed_today: number
  completed_sessions_today?: number
  failed_today: number
  failed_sessions_today?: number
  interrupted_sessions_today?: number
  today_tokens: number | null
  today_tool_calls: number | null
  task_status_distribution: Record<string, number>
  employee_status_distribution: Record<string, number>
}

export interface DashboardOverview {
  employee_total: number
  team_count: number
  working_employees: number
  running_tasks: number
  completed_today: number
  attention_total: number
  waiting_tasks: number
  failed_tasks: number
  online_employees: number
}

export interface DashboardEmployee extends RuntimeProjection {
  id: string
  name: string
  role: string
  character_id: number
  status: string
  enabled: boolean
  state: 'working' | 'idle' | 'waiting' | 'error'
  state_label: string
  current_task: {
    id: string
    title: string
    status: string
    current_session_id: string | null
    session_status: string | null
    runtime_status_label: string
    runtime_detail: string
    human_required_reason: string | null
    runtime_connection: string
  } | null
}

export interface DashboardTeam {
  id: string
  name: string
  order: number
  employee_count: number
  employees: DashboardEmployee[]
}

export interface DashboardEmployees {
  teams: DashboardTeam[]
}

export interface DashboardAttentionItem {
  task_id: string
  task_title: string
  employee_id: string | null
  employee_name: string
  status: string
  status_label: string
  current_session_id: string | null
  session_status: string | null
  runtime_detail: string
  human_required_reason: string | null
  runtime_connection: string
  occurred_at: string
}

export interface DashboardAttention {
  items: DashboardAttentionItem[]
}

export interface DashboardTaskTrendPoint {
  date: string
  total: number
  completed: number
}

export interface DashboardTasks {
  running: number
  waiting: number
  completed_today: number
  failed_today: number
  trend: DashboardTaskTrendPoint[]
  recent: Array<{
    id: string
    title: string
    status: string
    employee_id: string | null
    employee_name: string
    runtime_state: string
    runtime_status_label: string
    updated_at: string
  }>
}

export interface DashboardActivityItem {
  id: number
  employee_id: string | null
  employee_name: string
  kind: 'task_created' | 'task_started' | 'task_completed' | 'task_failed' | 'waiting_human' | 'schedule_triggered' | 'employee_created'
  description: string
  created_at: string
  target_type: 'task' | 'employee' | null
  target_id: string | null
}

export interface DashboardActivity {
  items: DashboardActivityItem[]
}

export interface DashboardKnowledge {
  today_added: number
  candidate_experiences: number
  total: number
  recent: Array<{ id: string; title: string }>
}

export interface LibraryServiceStatus {
  enabled: boolean
  healthy: boolean
  message: string
  last_synced_at: string | null
  pending_count: number
  failed_count: number
}

export interface TaskWorkspaceBinding {
  task_id: string
  task_status: TaskStatus
  runtime: {
    server: string
    session_id: string | null
    workspace: string | null
    connection: string
    error: string | null
    opencode_version: string
    workspace_ui: string | null
    workspace_ui_error: string | null
  }
  sessions: Array<{
    id: string
    workspace: string
    created_at: string
    status: string
    superseded_by: string | null
    finished_at: string | null
    current: boolean
    opencode_version: string
    workspace_ui: string | null
    workspace_ui_error: string | null
  }>
}
