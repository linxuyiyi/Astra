import { ApiError } from './client'
import { workerFacingMessage } from '@/utils/format'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export const MAINTENANCE_EVENT_PAGE_SIZE = 200

export type MaintenanceAction = 'check' | 'deploy' | 'update' | 'restart' | 'disable' | 'restore'

export type MaintenanceState =
  | 'QUEUED'
  | 'PREFLIGHT'
  | 'RUNNING'
  | 'VERIFYING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELED'
  | 'INTERRUPTED'

export interface WorkerBlocking {
  code: string
  message: string
  task_id?: string | null
  session_id?: string | null
}

export interface WorkerEquipmentItem {
  key: string
  label: string
  configured: boolean
  deployed: boolean
  verified: boolean | null
  desired_revision: string | null
  actual_revision: string | null
  diff: string[]
  error: string | null
  detail: string | null
}

export interface WorkerRepositoryHealth {
  id: string
  url: string | null
  target_branch: string | null
  branch: string | null
  commit: string | null
  dirty: boolean | null
  authentication: string | null
  error: string | null
}

export interface WorkerHealth {
  environment_id: number
  employee_id: string
  enabled: boolean
  connection: {
    channel: string
    state: string
    last_heartbeat_at: string | null
  }
  runtime_health: {
    status: string
    last_checked_at: string | null
    equipment: WorkerEquipmentItem[]
  }
  maintenance: {
    state: MaintenanceState | string
    operation_id: string
    stage: string
    attempt?: number
  } | null
  repository: WorkerRepositoryHealth | null
  schedulable: boolean
  blocking: WorkerBlocking[]
}

export interface MaintenanceTargetRevision {
  opencode_version: string | null
  worker_agent_version: string | null
  model_name: string | null
  branch: string | null
  skills: string[]
  mcp: string[]
  capability_revision: string | null
  repository_commit: string | null
}

export interface MaintenanceRepositorySnapshot {
  url: string | null
  branch: string | null
  commit: string | null
  dirty: boolean | null
  remote_commit: string | null
}

export interface MaintenanceChanged {
  runtime: boolean
  configuration: boolean
  skills: boolean
  repository: boolean
}

export interface MaintenanceAfterSnapshot {
  opencode_version: string | null
  worker_agent_version: string | null
  model_name: string | null
  repository: MaintenanceRepositorySnapshot | null
  capability_revision: string | null
  equipment: WorkerEquipmentItem[]
  changed: MaintenanceChanged
}

export interface MaintenanceEventPage {
  items: MaintenanceEvent[]
  next_cursor: number | null
}

export interface MaintenanceEvent {
  id: number
  operation_id: string
  attempt: number
  type: string
  stage: string | null
  name: string | null
  state: string | null
  message: string
  detail: Record<string, unknown> | null
  created_at: string
}

export interface MaintenanceOperation {
  id: string
  environment_id: number
  action: MaintenanceAction | string
  state: MaintenanceState | string
  stage: string
  attempt: number
  requested_by: string
  requested_at: string
  started_at: string | null
  finished_at: string | null
  cancel_requested: boolean
  error_code: string | null
  error_message: string | null
  target_revision: MaintenanceTargetRevision
  after_snapshot: Partial<MaintenanceAfterSnapshot>
  events?: MaintenanceEvent[]
}

export type MaintenanceSubmitResult = MaintenanceOperation

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!response.ok) throw new ApiError(await errorMessage(response), response.status)
  return (await response.json()) as T
}

async function errorMessage(response: Response): Promise<string> {
  const fallback = response.statusText || '请求失败'
  try {
    const payload = (await response.json()) as { detail?: unknown }
    return parseErrorDetail(payload.detail) ?? workerFacingMessage(fallback)
  } catch {
    return workerFacingMessage(fallback)
  }
}

export function parseErrorDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail) return maintenanceText(detail)
  if (!isRecord(detail)) return null
  const message = typeof detail.message === 'string' && detail.message ? detail.message : ''
  const blocking = parseBlocking(detail.blocking)
  if (message) {
    return maintenanceText([message, blocking].filter(Boolean).join('：'))
  }
  if (blocking) return maintenanceText(blocking)
  const code = typeof detail.code === 'string' ? detail.code : ''
  return code ? maintenanceText(code) : null
}

function parseBlocking(value: unknown): string {
  if (!Array.isArray(value)) return ''
  return value
    .flatMap((item) => {
      if (!isRecord(item)) return []
      const message = typeof item.message === 'string' ? item.message : typeof item.code === 'string' ? item.code : ''
      const task = typeof item.task_id === 'string' ? `任务 ${item.task_id}` : ''
      const session = typeof item.session_id === 'string' ? `会话 ${item.session_id}` : ''
      return [message, task, session].filter(Boolean).join('，')
    })
    .filter(Boolean)
    .join('；')
}

function maintenanceText(value: string): string {
  return workerFacingMessage(redactMaintenanceText(value))
}

export function getWorkerHealth(environmentId: number, signal?: AbortSignal): Promise<WorkerHealth> {
  return request<WorkerHealth>(`/api/agent-workers/${environmentId}/health`, { signal })
}

export function listMaintenanceOperations(
  environmentId: number,
  limit = 20,
  signal?: AbortSignal,
): Promise<{ items: MaintenanceOperation[] }> {
  return request<{ items: MaintenanceOperation[] }>(
    `/api/agent-workers/${environmentId}/maintenance?limit=${limit}`,
    { signal },
  )
}

export function submitMaintenance(
  environmentId: number,
  action: MaintenanceAction,
): Promise<MaintenanceSubmitResult> {
  return request<MaintenanceSubmitResult>(`/api/agent-workers/${environmentId}/maintenance`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
}

export function getMaintenanceOperation(
  operationId: string,
  signal?: AbortSignal,
): Promise<MaintenanceOperation> {
  return request<MaintenanceOperation>(`/api/maintenance/${encodeURIComponent(operationId)}`, { signal })
}

export function getMaintenanceEventsPage(
  operationId: string,
  afterId = 0,
  limit = MAINTENANCE_EVENT_PAGE_SIZE,
  signal?: AbortSignal,
): Promise<MaintenanceEventPage> {
  const query = new URLSearchParams({ after_id: String(Math.max(0, afterId)), limit: String(limit) })
  return request<MaintenanceEventPage>(
    `/api/maintenance/${encodeURIComponent(operationId)}/events/page?${query}`,
    { signal },
  )
}

export function cancelMaintenance(operationId: string): Promise<MaintenanceOperation> {
  return request<MaintenanceOperation>(
    `/api/maintenance/${encodeURIComponent(operationId)}/cancel`,
    { method: 'POST' },
  )
}

export function retryMaintenance(operationId: string): Promise<MaintenanceOperation> {
  return request<MaintenanceOperation>(
    `/api/maintenance/${encodeURIComponent(operationId)}/retry`,
    { method: 'POST' },
  )
}

export interface MaintenanceEventSubscription {
  signal: AbortSignal
  afterId?: number
  lastEventId?: string
  onEvent: (event: MaintenanceEvent) => void
  onEnd?: () => void
  onKeepalive?: () => void
}

export async function subscribeMaintenanceEvents(
  operationId: string,
  options: MaintenanceEventSubscription,
): Promise<void> {
  const query = options.afterId !== undefined ? `?after_id=${Math.max(0, options.afterId)}` : ''
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  if (options.lastEventId) headers['Last-Event-ID'] = options.lastEventId
  const response = await fetch(
    `${API_BASE}/api/maintenance/${encodeURIComponent(operationId)}/events${query}`,
    { headers, signal: options.signal, cache: 'no-store' },
  )
  if (!response.ok || !response.body) {
    throw new ApiError(await errorMessage(response), response.status)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (!options.signal.aborted) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n')
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? ''
      for (const frame of frames) {
        const parsed = parseEventFrame(frame)
        if (!parsed) continue
        if (parsed.event === 'end') {
          options.onEnd?.()
          return
        }
        if (parsed.keepalive) {
          options.onKeepalive?.()
          continue
        }
        if (!parsed.data) continue
        try {
          const payload = JSON.parse(parsed.data) as MaintenanceEvent & { type?: string }
          if (payload.type === 'keepalive') {
            options.onKeepalive?.()
            continue
          }
          if (typeof payload.id !== 'number') continue
          options.onEvent(payload)
        } catch {
          /* 忽略单条格式错误的事件，保持订阅 */
        }
      }
    }
    if (!options.signal.aborted) {
      throw new Error('维护日志连接提前结束，请重新订阅')
    }
  } finally {
    await reader.cancel().catch(() => undefined)
  }
}

function parseEventFrame(frame: string): { event: string; data: string; keepalive: boolean } | null {
  if (!frame.trim()) return null
  let event = 'message'
  let data = ''
  let keepalive = false
  for (const line of frame.split('\n')) {
    if (line.startsWith(':')) {
      keepalive = true
      continue
    }
    const index = line.indexOf(':')
    const field = index === -1 ? line : line.slice(0, index)
    const value = index === -1 ? '' : line.slice(index + 1).replace(/^ /, '')
    if (field === 'event') event = value
    else if (field === 'data') data = data ? `${data}\n${value}` : value
  }
  return { event, data, keepalive }
}

const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/(https?:\/\/)[^/@\s]+@/gi, '$1[已隐藏]@'],
  [/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [已隐藏]'],
  [/Authorization\s*[:=]\s*[^\s,;]+/gi, 'Authorization: [已隐藏]'],
  [/((?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+/gi, '$1[已隐藏]'],
  [/\bsk-[A-Za-z0-9_-]{8,}\b/g, '[已隐藏]'],
]

export function redactMaintenanceText(value: string | null | undefined): string {
  let text = String(value ?? '')
  for (const [pattern, replacement] of SECRET_PATTERNS) text = text.replace(pattern, replacement)
  return text
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
