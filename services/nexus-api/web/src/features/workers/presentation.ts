import { workerDisplayName } from '@/utils/workerName'
import type { Employee, ExecutionEnvironment, Task } from '@/api/types'
import type {
  MaintenanceEvent,
  MaintenanceOperation,
  WorkerBlocking,
  WorkerEquipmentItem,
  WorkerHealth,
} from '@/api/maintenance'
import { redactMaintenanceText } from '@/api/maintenance'
import { employeeDisplayName, statusLabel, statusTone, workerFacingMessage } from '@/utils/format'
import { maintenanceSemanticTone, semanticTone, type SemanticTone } from './semantic'
import { occupiedTaskIds } from './actionPolicy'

const ACTION_LABELS: Record<string, string> = {
  check: '检查',
  deploy: '一键部署',
  update: '更新',
  restart: '重启',
}

const STATE_LABELS: Record<string, string> = {
  QUEUED: '排队中',
  RUNNING: '执行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELED: '已取消',
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
  verify_failed: '装备验证失败',
  blocked: '被阻塞',
  finished: '完成',
  failed: '失败',
  canceled: '已取消',
}

const ERROR_CODE_LABELS: Record<string, string> = {
  lease_expired: '维护租约已过期，可重新检查或一键部署',
  controller_stopped: '维护进程已停止，可重新检查或一键部署',
  operation_not_finished: '维护未完成，可重新检查或一键部署',
  canceled: '操作已取消',
  health_check_failed: '健康检查未通过',
  worker_not_installed: '尚未安装，请先执行一键部署',
}

const ACTIVE_STATES = new Set(['QUEUED', 'RUNNING'])
const RETRYABLE_STATES = new Set(['FAILED', 'CANCELED'])

export function maintenanceActionLabel(action: string | null | undefined): string {
  return ACTION_LABELS[String(action ?? '')] ?? '维护'
}

export function maintenanceStateLabel(state: string | null | undefined): string {
  return STATE_LABELS[String(state ?? '')] ?? '无记录'
}

export function maintenanceStateTone(state: string | null | undefined): SemanticTone {
  return maintenanceSemanticTone(state)
}

export function maintenanceStageLabel(stage: string | null | undefined): string {
  const value = String(stage ?? '')
  return STAGE_LABELS[value] ?? (value ? maintenanceCopy(value) : '—')
}

export function isMaintenanceActive(state: string | null | undefined): boolean {
  return ACTIVE_STATES.has(String(state ?? ''))
}

export function canCancelMaintenance(operation: MaintenanceOperation | null | undefined): boolean {
  return Boolean(operation && ACTIVE_STATES.has(operation.state))
}

export function canRetryMaintenance(operation: MaintenanceOperation | null | undefined): boolean {
  return Boolean(operation && Object.hasOwn(ACTION_LABELS, operation.action) && RETRYABLE_STATES.has(operation.state))
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

const UNRECORDED_REQUESTERS = new Set(['console-admin', 'api'])

/** 旧版本没有接入 UID，历史值保留原样并明确标注，不追填成当前用户。 */
export function requesterLabel(value: string | null | undefined): string {
  const text = String(value ?? '').trim()
  if (!text || UNRECORDED_REQUESTERS.has(text)) return '历史记录，UID 未记录'
  return text
}

export function connectionLabel(state: string | null | undefined): string {
  if (state === 'online') return '在线'
  if (state === 'offline') return '离线'
  if (state === 'local') return '本机'
  return '未获取'
}

export function connectionTone(state: string | null | undefined): SemanticTone {
  if (state === 'online') return 'success'
  if (state === 'offline') return 'danger'
  return 'neutral'
}

export function readinessLabel(state: string | null | undefined): string {
  return ({ READY: '就绪', DRIFTED: '待同步', DEGRADED: '运行异常', UNKNOWN: '待检查' } as Record<string, string>)[String(state ?? '')] ?? '未获取'
}

export function readinessTone(state: string | null | undefined): SemanticTone {
  return ({ READY: 'success', DRIFTED: 'attention', DEGRADED: 'danger', UNKNOWN: 'neutral' } as Record<string, SemanticTone>)[String(state ?? '')] ?? 'neutral'
}

export function runtimeHealthLabel(health: WorkerHealth | null): string {
  const status = health?.runtime_health?.status
  return status ? statusLabel(status) : '未获取'
}

export function runtimeHealthTone(health: WorkerHealth | null): SemanticTone {
  const status = health?.runtime_health?.status
  if (!status) return 'neutral'
  // 通用 OFFLINE 在员工列表里是中性；运行环境离线在这里是失败事实。
  if (status === 'OFFLINE') return 'danger'
  return semanticTone(statusTone(status))
}

export function schedulableLabel(health: WorkerHealth | null): string {
  if (!health) return '未获取'
  return health.schedulable ? '可接任务' : '暂不可接任务'
}

export function schedulableTone(health: WorkerHealth | null): SemanticTone {
  if (!health) return 'neutral'
  return health.schedulable ? 'success' : 'attention'
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

export function equipmentTone(value: boolean | null): SemanticTone {
  if (value === true) return 'success'
  if (value === false) return 'danger'
  return 'neutral'
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

export function eventTone(event: MaintenanceEvent): SemanticTone {
  return maintenanceSemanticTone(event.state, event.type)
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
  currentTaskId: string | null
  waitingTask?: Task
  health: WorkerHealth | null
  connection: string
  enabled: boolean | null
  maintenanceState: string | null
  repository: WorkerRepositoryView
}

export function toWorkerRow(
  employee: Employee,
  environment: ExecutionEnvironment,
  tasks: Task[],
  health: WorkerHealth | null,
): WorkerRow {
  const currentTaskId = occupiedTaskIds(health)[0] ?? null
  const currentTask = tasks.find(task => task.id === currentTaskId)
  return {
    id: employee.id,
    employee,
    environment,
    name: workerDisplayName({ id: employee.id, execution_environment: environment }),
    employeeName: employeeDisplayName(employee.name),
    role: employee.role,
    server: serverAddress(environment),
    currentTask,
    currentTaskId,
    waitingTask: currentTask?.status === 'WAITING' ? currentTask : undefined,
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
