import { isRuntimeState, runtimeStatusLabel, runtimeStatusTone } from '@/features/runtime/status'

const STATUS_LABEL: Record<string, string> = {
  OFFLINE: '离线', IDLE: '空闲', WORKING: '工作中', COLLABORATING: '协作中', NEED_HUMAN: '等待人工', WAIT_HUMAN: '需要人工处理', PAUSED: '已暂停', ERROR: '异常',
  AVAILABLE: '正常', CHECKING: '检查中', UNAVAILABLE: '不可用', UNKNOWN: '未检查',
}

export function safeUUID(): string {
  const webCrypto = globalThis.crypto
  if (typeof webCrypto?.randomUUID === 'function') return webCrypto.randomUUID()
  const bytes = new Uint8Array(16)
  if (typeof webCrypto?.getRandomValues === 'function') webCrypto.getRandomValues(bytes)
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256)
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const value = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${value.slice(0, 8)}-${value.slice(8, 12)}-${value.slice(12, 16)}-${value.slice(16, 20)}-${value.slice(20)}`
}

export const statusLabel = (value: string) => isRuntimeState(value) ? runtimeStatusLabel(value) : (STATUS_LABEL[value] ?? '未知状态')
export const taskTypeLabel = (value: string) => value === 'ISSUE' ? '问题' : value === 'REQUIREMENT' ? '需求' : '未知类型'
export const priorityLabel = (value: string) => ({ VERY_HIGH: '很高', HIGH: '高', MEDIUM: '中', LOW: '低' }[value] ?? '未设置')
export const acceptanceStatusLabel = (value: string) => ({ SATISFIED: '满足', UNSATISFIED: '未满足', UNCONFIRMED: '无法确认' }[value] ?? '无法确认')

const STATUS_TAG_TYPES = { info: 'primary', success: 'success', warning: 'warning', danger: 'danger', neutral: 'info' } as const
export const statusTagType = (status: string) => STATUS_TAG_TYPES[statusTone(status)]

export function statusTone(status: string): 'info' | 'success' | 'warning' | 'danger' | 'neutral' {
  if (['WORKING', 'COLLABORATING', 'AVAILABLE'].includes(status)) return 'success'
  if (['WAIT_HUMAN', 'NEED_HUMAN', 'WAITING', 'PAUSED'].includes(status)) return 'warning'
  if (['ERROR', 'UNAVAILABLE'].includes(status)) return 'danger'
  if (['IDLE', 'OFFLINE', 'UNKNOWN'].includes(status)) return 'neutral'
  if (status === 'CHECKING') return 'info'
  return isRuntimeState(status) ? runtimeStatusTone(status) : 'neutral'
}

const parseApiTime = (value: string) => new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)

export const formatTime = (value: string | null | undefined) =>
  value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(parseApiTime(value)) : '—'

export const formatDuration = (from: string | null, to?: string | null) => {
  if (!from) return '—'
  const milliseconds = (to ? parseApiTime(to) : new Date()).getTime() - parseApiTime(from).getTime()
  const minutes = Math.max(0, Math.floor(milliseconds / 60_000))
  if (minutes === 0) return '不足 1 分钟'
  if (minutes < 60) return `${minutes} 分钟`
  const remainingMinutes = minutes % 60
  return `${Math.floor(minutes / 60)} 小时${remainingMinutes ? ` ${remainingMinutes} 分钟` : ''}`
}

export const formatTaskDuration = (task: { status: string; started_at: string | null; finished_at: string | null; last_activity_at: string | null }) =>
  formatDuration(task.started_at, task.finished_at ?? (task.status === 'RUNNING' ? undefined : task.last_activity_at))

export const formatTokens = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 2)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

export const employeeDisplayName = (value: string | null | undefined) =>
  String(value ?? 'Worker').replace(/^(?:Astra|员工)-(\d+)$/i, 'Worker-$1')

export const workerFacingMessage = (value: string) =>
  value.replaceAll('数字员工', 'Worker').replaceAll('员工', 'Worker')

export const formatDurationSeconds = (seconds: number | null | undefined) => {
  if (seconds === null || seconds === undefined) return '—'
  const minutes = Math.floor(seconds / 60)
  if (minutes === 0) return '不足 1 分钟'
  if (minutes < 60) return `${minutes} 分钟`
  const remainingMinutes = minutes % 60
  return `${Math.floor(minutes / 60)} 小时${remainingMinutes ? ` ${remainingMinutes} 分钟` : ''}`
}

export const formatMilliseconds = (milliseconds: number | null | undefined) => {
  if (milliseconds === null || milliseconds === undefined) return '—'
  return milliseconds < 1000 ? `${milliseconds} 毫秒` : `${(milliseconds / 1000).toFixed(1)} 秒`
}

export const environmentLabel = (snapshot: { mode?: unknown; name?: unknown; host?: unknown } | null | undefined) => {
  if (!snapshot) return '未配置'
  return snapshot.mode === 'LINUX_REMOTE' ? `Linux · ${snapshot.name ?? snapshot.host ?? '远程'}` : '本地'
}

export const executionModeLabel = (value: unknown) =>
  value === 'LINUX_REMOTE' ? 'Linux 远程' : value === 'LOCAL' ? '本地' : '—'

export const executorLabel = (value: string) =>
  value === 'opencode_server'
    ? 'OpenCode Server'
    : value === 'opencode_server_ssh'
      ? 'Linux 远程 OpenCode Server'
      : value === 'opencode_cli_fallback_ssh'
        ? 'Linux 远程 CLI fallback'
      : value === 'opencode_cli_fallback'
        ? '本地 CLI fallback'
        : '未知执行方式'

export const sourceLabel = (source: string) => source === 'nr_test_system' ? 'nr_test_system' : source === 'manual' ? '手工创建' : '其他来源'

export const evidenceKindLabel = (value: string) =>
  value === 'work_record' ? 'Agent 工作记录' : value === 'log' ? '运行日志' : value === 'attachment' ? '结果附件' : '其他证据'

export const platformCopy = (value: string | null | undefined) =>
  String(value ?? '')
    .replace(/^completed$/i, '任务已完成')
    .replace(/^failed$/i, '任务未完成')
    .replace(/^running$/i, '任务处理中')
    .replaceAll('Agent 工作环境检查完成', '工作环境检查完成')
    .replaceAll('Agent 工作环境已更新', '工作环境已更新')
    .replaceAll('Agent 返回模型用量事件', '模型用量已更新')
    .replaceAll('OpenCode Session', '执行会话')
    .replaceAll('OpenCode 会话', '执行会话')
    .replaceAll('CLI 执行会话', '执行会话')
    .replaceAll('CLI 会话', '执行会话')
    .replaceAll('-opencode.json', '-agent.json')
    .replaceAll('-cli.json', '-agent.json')
    .replaceAll('OpenCode', '执行工具')
    .replaceAll('CLI', '执行工具')