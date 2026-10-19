import type { Employee, ExecutionEnvironment, Task } from '@/api/types'
import type {
  MaintenanceEvent,
  MaintenanceOperation,
  WorkerBlocking,
  WorkerEquipmentItem,
  WorkerHealth,
} from '@/api/maintenance'
import { redactMaintenanceText } from '@/api/maintenance'
import { employeeDisplayName, statusLabel, statusTagType, workerFacingMessage } from '@/utils/format'

export type TagTone = 'primary' | 'success' | 'warning' | 'danger' | 'info'

const ACTION_LABELS: Record<string, string> = {
  check: '检查',
  deploy: '一键部署',
  update: '更新',
  restart: '重启',
  disable: '禁用',
  restore: '恢复',
}

const STATE_LABELS: Record<string, string> = {
  QUEUED: '排队中',
  PREFLIGHT: '预检中',
  RUNNING: '执行中',
  VERIFYING: '验证中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELED: '已取消',
  INTERRUPTED: '已中断待核对',
}

const STATE_TONES: Record<string, TagTone> = {
  QUEUED: 'info',
  PREFLIGHT: 'info',
  RUNNING: 'primary',
  VERIFYING: 'primary',
  SUCCEEDED: 'success',
  FAILED: 'danger',
  CANCELED: 'info',
  INTERRUPTED: 'warning',
}

const STAGE_LABELS: Record<string, string> = {
  queued: '等待执行',
  preflight: '预检',
  checking: '只读检查',
  configure: '写入配置',
  install: '安装与配置',
  system: '系统依赖',
  runtime: 'Node.js 与 npm',
  repository: '工程仓库',
  opencode: 'OpenCode 安装',
  write_check: '写入前检查',
  channel: '连接 Worker',
  restart: '重启 OpenCode',
  verify: '验证',
  reconcile: '核对远端',
  reconciled: '已核对',
  blocked: '被阻塞',
  finished: '完成',
  failed: '失败',
  canceled: '已取消',
  interrupted: '已中断',
}

const ERROR_CODE_LABELS: Record<string, string> = {
  lease_expired: '维护执行已中断，需核对远端状态',
  controller_stopped: '维护执行已中断，需核对远端状态',
  operation_not_finished: '维护执行已中断，需核对远端状态',
  remote_state_unknown: '维护执行已中断，需核对远端状态',
  canceled_after_remote_change: '取消时远端可能已变更，需核对远端状态',
  canceled: '操作已取消',
  health_check_failed: '只读健康检查未通过',
}

const ACTIVE_STATES = new Set(['QUEUED', 'PREFLIGHT', 'RUNNING', 'VERIFYING'])
const RETRYABLE_STATES = new Set(['FAILED', 'INTERRUPTED', 'CANCELED'])

export function maintenanceActionLabel(action: string | null | undefined): string {
  return ACTION_LABELS[String(action ?? '')] ?? '维护'
}

export function maintenanceStateLabel(state: string | null | undefined): string {
  return STATE_LABELS[String(state ?? '')] ?? '无记录'
}

export function maintenanceStateTone(state: string | null | undefined): TagTone {
  return STATE_TONES[String(state ?? '')] ?? 'info'
}

export function maintenanceStageLabel(stage: string | null | undefined): string {
  const value = String(stage ?? '')
  return STAGE_LABELS[value] ?? (value ? maintenanceCopy(value) : '—')
}

export function isMaintenanceActive(state: string | null | undefined): boolean {
  return ACTIVE_STATES.has(String(state ?? ''))
}

export function canCancelMaintenance(operation: MaintenanceOperation | null | undefined): boolean {
  return Boolean(operation && (ACTIVE_STATES.has(operation.state) || operation.state === 'INTERRUPTED'))
}

export function canRetryMaintenance(operation: MaintenanceOperation | null | undefined): boolean {
  return Boolean(operation && RETRYABLE_STATES.has(operation.state))
}

export function maintenanceCopy(value: string | null | undefined): string {
  return workerFacingMessage(redactMaintenanceText(value)).replaceAll('SubAgent', 'Worker')
}

export function operationReason(operation: MaintenanceOperation | null | undefined): string {
  if (!operation) return ''
  const mapped = operation.error_code ? ERROR_CODE_LABELS[operation.error_code] : ''
  if (mapped) return mapped
  return maintenanceCopy(operation.error_message || operation.error_code || '')
}

export function connectionLabel(state: string | null | undefined): string {
  if (state === 'online') return '在线'
  if (state === 'offline') return '离线'
  if (state === 'local') return '本机'
  return '未获取'
}

export function connectionTone(state: string | null | undefined): TagTone {
  if (state === 'online') return 'success'
  if (state === 'offline') return 'danger'
  if (state === 'local') return 'primary'
  return 'info'
}

export function runtimeHealthLabel(health: WorkerHealth | null): string {
  const status = health?.runtime_health?.status
  return status ? statusLabel(status) : '未获取'
}

export function runtimeHealthTone(health: WorkerHealth | null): TagTone {
  const status = health?.runtime_health?.status
  return status ? (statusTagType(status) as TagTone) : 'info'
}

export function schedulableLabel(health: WorkerHealth | null): string {
  if (!health) return '未获取'
  return health.schedulable ? '可调度' : '不可调度'
}

export function schedulableTone(health: WorkerHealth | null): TagTone {
  if (!health) return 'info'
  return health.schedulable ? 'success' : 'warning'
}

export function serverAddress(environment: ExecutionEnvironment | null | undefined): string {
  if (!environment) return '—'
  if (environment.mode !== 'LINUX_REMOTE') return '本机'
  const host = environment.host || '未配置主机'
  return environment.username ? `${environment.username}@${host}:${environment.port}` : `${host}:${environment.port}`
}

export interface WorkerRepositoryView {
  available: boolean
  configured: boolean
  id: string | null
  url: string | null
  targetBranch: string | null
  branch: string | null
  commit: string | null
  dirty: boolean | null
  authentication: string | null
  error: string | null
}

export function workerRepository(
  environment: ExecutionEnvironment | null | undefined,
  health: WorkerHealth | null,
): WorkerRepositoryView {
  const repository = health?.repository ?? null
  const id = repository?.id ?? environment?.repository_id ?? null
  const url = repository?.url ?? null
  const targetBranch = repository?.target_branch ?? environment?.branch ?? null
  return {
    available: Boolean(health),
    configured: Boolean(repository || id || targetBranch),
    id,
    url: url ? maintenanceCopy(url) : null,
    targetBranch,
    branch: repository?.branch ?? null,
    commit: repository?.commit ?? null,
    dirty: repository?.dirty ?? null,
    authentication: repository?.authentication ? maintenanceCopy(repository.authentication) : null,
    error: repository?.error ? maintenanceCopy(repository.error) : null,
  }
}

export function repositorySummary(repository: WorkerRepositoryView): string {
  if (!repository.configured) return repository.available ? '未绑定代码仓库' : '未获取'
  if (repository.url) return repository.url
  if (repository.id) return `仓库 ${repository.id}`
  return repository.targetBranch ? `分支 ${repository.targetBranch}` : '已绑定仓库'
}

export function equipmentItems(health: WorkerHealth | null): WorkerEquipmentItem[] {
  return (health?.runtime_health?.equipment ?? []).map((item) => ({
    ...item,
    diff: item.diff.map((value) => maintenanceCopy(value)),
    error: item.error ? maintenanceCopy(item.error) : null,
    detail: item.detail ? maintenanceCopy(item.detail) : null,
  }))
}

export function equipmentStateLabel(value: boolean | null): string {
  if (value === true) return '是'
  if (value === false) return '否'
  return '未核验'
}

export function equipmentTone(value: boolean | null): TagTone {
  if (value === true) return 'success'
  if (value === false) return 'danger'
  return 'info'
}

export function equipmentDiff(item: WorkerEquipmentItem): string {
  return item.diff.length ? item.diff.join('、') : ''
}

export function revisionRows(operation: MaintenanceOperation | null | undefined) {
  if (!operation) return []
  const target = operation.target_revision
  const after = operation.after_snapshot
  return [
    { key: 'opencode', label: 'OpenCode', target: target.opencode_version, actual: after.opencode_version ?? null },
    { key: 'worker_agent', label: 'Worker Agent', target: target.worker_agent_version, actual: after.worker_agent_version ?? null },
    { key: 'model', label: '模型', target: target.model_name, actual: after.model_name ?? null },
    { key: 'repository', label: '代码仓库提交', target: target.repository_commit, actual: after.repository?.commit ?? null },
    { key: 'bindings', label: 'Skill / MCP 修订', target: target.capability_revision, actual: after.capability_revision ?? null },
  ]
}

export function changedLabels(operation: MaintenanceOperation | null | undefined): string[] {
  const changed = operation?.after_snapshot.changed
  if (!changed) return []
  const labels: Array<[keyof typeof changed, string]> = [
    ['runtime', '运行时'],
    ['configuration', '配置'],
    ['skills', '技能'],
    ['repository', '仓库'],
  ]
  const items = labels.filter(([key]) => changed[key]).map(([, label]) => label)
  return items.length ? items : ['无变更']
}

export function eventText(event: MaintenanceEvent): string {
  return maintenanceCopy(event.message || event.name || event.type)
}

export function eventDetailText(event: MaintenanceEvent): string {
  if (!event.detail || !Object.keys(event.detail).length) return ''
  return maintenanceCopy(JSON.stringify(event.detail, null, 2))
}

export function eventStateLabel(event: MaintenanceEvent): string {
  const state = String(event.state ?? '')
  if (state && STATE_LABELS[state]) return STATE_LABELS[state]
  const labels: Record<string, string> = { passed: '通过', failed: '失败', running: '执行中', success: '成功' }
  return labels[state] ?? maintenanceCopy(state || event.type)
}

export function eventTone(event: MaintenanceEvent): TagTone {
  const state = String(event.state ?? '')
  if (state === 'failed' || state === 'FAILED' || event.type === 'interrupted') return 'danger'
  if (state === 'passed' || state === 'SUCCEEDED') return 'success'
  if (state === 'running' || state === 'RUNNING' || state === 'PREFLIGHT' || state === 'VERIFYING') return 'primary'
  if (event.type === 'canceled' || event.type === 'cancel_requested' || state === 'CANCELED' || state === 'INTERRUPTED') return 'warning'
  return 'info'
}

function blockingText(item: WorkerBlocking): string {
  const message = maintenanceCopy(item.message)
  const refs = [item.task_id ? `任务 ${item.task_id}` : '', item.session_id ? `会话 ${item.session_id}` : ''].filter(Boolean)
  return refs.length ? `${message}（${refs.join('，')}）` : message
}

export function blockingReasons(health: WorkerHealth | null): string[] {
  return (health?.blocking ?? []).map(blockingText)
}

export interface WorkerRow {
  id: string
  employee: Employee
  environment: ExecutionEnvironment
  name: string
  employeeName: string
  role: string
  server: string
  currentTask?: Task
  waitingTask?: Task
  health: WorkerHealth | null
  connection: string
  enabled: boolean | null
  maintenanceState: string | null
  repository: WorkerRepositoryView
}

const ACTIVE_TASK_STATES = new Set(['PENDING', 'RUNNING', 'WAITING'])

export function toWorkerRow(
  employee: Employee,
  environment: ExecutionEnvironment,
  tasks: Task[],
  health: WorkerHealth | null,
): WorkerRow {
  const workerTasks = tasks.filter(
    (task) => (task.employee_id === employee.id || task.worker_id === employee.id) && ACTIVE_TASK_STATES.has(task.status),
  )
  return {
    id: employee.id,
    employee,
    environment,
    name: environment.name || employeeDisplayName(employee.name),
    employeeName: employeeDisplayName(employee.name),
    role: employee.role,
    server: serverAddress(environment),
    currentTask:
      workerTasks.find((task) => task.status === 'RUNNING') ??
      workerTasks.find((task) => task.status === 'WAITING') ??
      workerTasks.find((task) => task.status === 'PENDING'),
    waitingTask: workerTasks.find((task) => task.status === 'WAITING'),
    health,
    connection: health?.connection.state ?? 'unknown',
    enabled: health?.enabled ?? null,
    maintenanceState: health?.maintenance?.state ?? null,
    repository: workerRepository(environment, health),
  }
}

export function workerMatches(row: WorkerRow, query: string): boolean {
  const normalized = query.trim().toLocaleLowerCase('zh-CN')
  if (!normalized) return true
  return [
    row.name,
    row.employeeName,
    row.employee.id,
    row.environment.id,
    row.role,
    row.server,
    row.repository.url,
    row.repository.targetBranch,
    row.currentTask?.title,
    row.waitingTask?.title,
  ].some((value) => String(value ?? '').toLocaleLowerCase('zh-CN').includes(normalized))
}
