import { isRuntimeState, runtimeStatusLabel, runtimeStatusTone } from '@/features/runtime/status'

const STATUS_LABEL: Record<string, string> = {
  OFFLINE: '离线', IDLE: '空闲', WORKING: '工作中', COLLABORATING: '协作中', NEED_HUMAN: '等待人工', WAIT_HUMAN: '需要人工处理', PAUSED: '已暂停', ERROR: '异常',
  AVAILABLE: '正常', DRIFTED: '待同步', CHECKING: '检查中', UNAVAILABLE: '不可用', UNKNOWN: '未检查',
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

export function statusTone(status: string): 'info' | 'success' | 'warning' | 'danger' | 'neutral' {
  if (isRuntimeState(status)) return runtimeStatusTone(status)
  if (['COLLABORATING', 'AVAILABLE'].includes(status)) return 'success'
  if (['WAIT_HUMAN', 'NEED_HUMAN', 'WAITING', 'PAUSED', 'DRIFTED'].includes(status)) return 'warning'
  if (['ERROR', 'UNAVAILABLE'].includes(status)) return 'danger'
  if (['IDLE', 'OFFLINE', 'UNKNOWN'].includes(status)) return 'neutral'
  if (status === 'CHECKING') return 'info'
  return 'neutral'
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

export const formatTokens = (value: number | null | undefined) => {
  if (value === null || value === undefined) return '—'
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 2)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`
  return String(value)
}

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

export const sourceLabel = (source: string) => source === 'nr_test_system' ? 'nr_test_system' : source === 'manual' ? '手工创建' : '其他来源'
