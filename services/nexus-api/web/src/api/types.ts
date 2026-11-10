export type TaskType = 'ISSUE' | 'REQUIREMENT'
export type TaskPriority = 'VERY_HIGH' | 'HIGH' | 'MEDIUM' | 'LOW'
export type TaskStatus = import('@/features/runtime/status').TaskState

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

export type TaskCategoryWrite = Pick<
  TaskCategory,
  | 'name'
  | 'description'
  | 'enabled'
  | 'sort_order'
  | 'task_prompt'
  | 'question_prompt'
>

export interface WorkerOccupiedTask {
  id: string
  title: string
}

export interface Worker {
  id: string
  name: string
  description: string
  enabled: boolean
  status: 'OFFLINE' | 'IDLE' | 'WORKING' | 'PAUSED'
  task_types: TaskType[]
  category_ids: string[]
  runtime_engine: 'opencode'
  mode: 'LOCAL' | 'LINUX_REMOTE'
  host: string | null
  port: number
  username: string | null
  has_ssh_password: boolean
  model_name: string | null
  workspace_root: string
  repository_url: string | null
  has_git_token: boolean
  branch: string | null
  skill_refs: string[]
  mcp_refs: Array<{
    name: string
    url: string
    config?: Record<string, unknown>
  }>
  opencode_port: number | null
  last_checked_at: string | null
  opencode_version: string | null
  occupied_tasks: WorkerOccupiedTask[]
  online: boolean | null
  created_at: string
  updated_at: string
}

export interface AcceptanceCriterion {
  id: string
  content: string
}

export interface Task {
  id: string
  title: string
  description: string
  task_type: TaskType
  category_id: string | null
  priority: TaskPriority
  acceptance_criteria: AcceptanceCriterion[]
  attachments: Array<Record<string, unknown>>
  source: string
  external_task_id: string | null
  origin_employee_id: string | null
  external_reference_id: string | null
  agent_worker_id: string | null
  worker_name?: string | null
  worker_online?: boolean | null
  worker_last_checked_at?: string | null
  current_session_id: string | null
  workspace: string | null
  last_message_id: string | null
  pending_message_id: string | null
  status: TaskStatus
  waiting_reason: string | null
  current_progress: string
  execution_released: boolean
  created_at: string
  updated_at: string
  claimed_at: string | null
  started_at: string | null
  finished_at: string | null
  last_activity_at: string | null
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

export interface ExperienceReviewJob {
  id: string
  task_id: string | null
  task_id_snapshot: string
  root_session_id: string
  status: string
  phase: string
  attempts: number
  source_message_count: number
  segment_count: number
  completed_segment_count: number
  candidate_ids: string[]
  zero_reason: string | null
  error_code: string | null
  error_message: string | null
  confirmed_at: string
  created_at: string
  finished_at: string | null
}

export type KnowledgeType =
  | 'document'
  | 'rule'
  | 'guide'
  | 'experience'
  | 'faq'
  | 'tool'
  | 'example'
export type KnowledgeStatus =
  | 'draft'
  | 'candidate'
  | 'published'
  | 'archived'
  | 'rejected'
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

export interface Metrics {
  running_tasks: number
  pending_tasks: number
  waiting_tasks: number
  idle_employees: number
  online_employees: number
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
    runtime_engine: 'opencode'
    server: string
    session_id: string | null
    workspace: string | null
    opencode_version: string
    workspace_ui: string | null
    workspace_ui_error: string | null
    worker_online: boolean
  }
}

export interface RelatedTask {
  id: string
  title: string
  status: string
  relation: string
  last_used_at: string | null
}
